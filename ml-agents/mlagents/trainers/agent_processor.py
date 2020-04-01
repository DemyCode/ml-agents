import sys
from typing import List, Dict, Deque, TypeVar, Generic, Tuple, Any
from collections import defaultdict, Counter, deque

from mlagents_envs.base_env import DecisionSteps, DecisionStep, TerminalSteps, TerminalStep
from mlagents.trainers.trajectory import Trajectory, AgentExperience
from mlagents.trainers.policy.tf_policy import TFPolicy
from mlagents.trainers.policy import Policy
from mlagents.trainers.action_info import ActionInfo, ActionInfoOutputs
from mlagents.trainers.stats import StatsReporter
from mlagents.trainers.brain_conversion_utils import get_global_agent_id

T = TypeVar("T")


class AgentProcessor:
    """
    AgentProcessor contains a dictionary per-agent trajectory buffers. The buffers are indexed by agent_id.
    Buffer also contains an update_buffer that corresponds to the buffer used when updating the model.
    One AgentProcessor should be created per agent group.
    """

    def __init__(
        self,
        policy: TFPolicy,
        behavior_id: str,
        stats_reporter: StatsReporter,
        max_trajectory_length: int = sys.maxsize,
    ):
        """
        Create an AgentProcessor.
        :param trainer: Trainer instance connected to this AgentProcessor. Trainer is given trajectory
        when it is finished.
        :param policy: Policy instance associated with this AgentProcessor.
        :param max_trajectory_length: Maximum length of a trajectory before it is added to the trainer.
        :param stats_category: The category under which to write the stats. Usually, this comes from the Trainer.
        """
        self.experience_buffers: Dict[str, List[AgentExperience]] = defaultdict(list)
        self.last_step_result: Dict[str, Tuple[DecisionStep, int]] = {}
        # last_take_action_outputs stores the action a_t taken before the current observation s_(t+1), while
        # grabbing previous_action from the policy grabs the action PRIOR to that, a_(t-1).
        self.last_take_action_outputs: Dict[str, ActionInfoOutputs] = {}
        # Note: In the future this policy reference will be the policy of the env_manager and not the trainer.
        # We can in that case just grab the action from the policy rather than having it passed in.
        self.policy = policy
        self.episode_steps: Counter = Counter()
        self.episode_rewards: Dict[str, float] = defaultdict(float)
        self.stats_reporter = stats_reporter
        self.max_trajectory_length = max_trajectory_length
        self.trajectory_queues: List[AgentManagerQueue[Trajectory]] = []
        self.behavior_id = behavior_id

    def add_experiences(
        self,
        decision_steps: DecisionSteps,
        terminal_steps: TerminalSteps,
        worker_id: int,
        previous_action: ActionInfo,
    ) -> None:
        """
        Adds experiences to each agent's experience history.
        :param decision_steps: current DecisionSteps.
        :param terminal_steps: current TerminalSteps.
        :param previous_action: The outputs of the Policy's get_action method.
        """
        take_action_outputs = previous_action.outputs
        if take_action_outputs:
            for _entropy in take_action_outputs["entropy"]:
                self.stats_reporter.add_stat("Policy/Entropy", _entropy)

        # Make unique agent_ids that are global across workers
        action_global_agent_ids = [
            get_global_agent_id(worker_id, ag_id) for ag_id in previous_action.agent_ids
        ]
        for global_id in action_global_agent_ids:
            if global_id in self.last_step_result:  # Don't store if agent just reset
                self.last_take_action_outputs[global_id] = take_action_outputs

        # Iterate over all the decision steps
        for ongoing_step in decision_steps.values():
            local_id = ongoing_step.agent_id
            global_id = get_global_agent_id(worker_id, local_id)
            stored_decision_step, idx = self.last_step_result.get(global_id, (None, None))
            stored_take_action_outputs = self.last_take_action_outputs.get(
                global_id, None
            )
            # Index is needed to grab from last_take_action_outputs
            self.last_step_result[global_id] = (
                ongoing_step,
                decision_steps.agent_id_to_index[local_id]
            )
            
            # This state is the consequence of a past action
            if stored_decision_step is not None and stored_take_action_outputs is not None:
                obs = stored_decision_step.obs
                if self.policy.use_recurrent:
                    memory = self.policy.retrieve_memories([global_id])[0, :]
                else:
                    memory = None
                done = False # Since this is an ongoing step
                max_step = False
                                    # Add the outputs of the last eval
                action = stored_take_action_outputs["action"][idx]
                if self.policy.use_continuous_act:
                    action_pre = stored_take_action_outputs["pre_action"][idx]
                else:
                    action_pre = None
                action_probs = stored_take_action_outputs["log_probs"][idx]
                action_mask = stored_decision_step.action_mask
                prev_action = self.policy.retrieve_previous_action([global_id])[
                    0, :
                ]
                experience = AgentExperience(
                    obs=obs,
                    reward=ongoing_step.reward,
                    done=done,
                    action=action,
                    action_probs=action_probs,
                    action_pre=action_pre,
                    action_mask=action_mask,
                    prev_action=prev_action,
                    max_step=max_step,
                    memory=memory,
                )
                # Add the value outputs if needed
                self.experience_buffers[global_id].append(experience)
                self.episode_rewards[global_id] += ongoing_step.reward
                self.episode_steps[global_id] += 1
            
                # if the trajectory is too long, we truncate it
                if len(self.experience_buffers[global_id]) >= self.max_trajectory_length:
                    # Make next AgentExperience
                    next_obs = ongoing_step.obs
                    trajectory = Trajectory(
                        steps=self.experience_buffers[global_id],
                        agent_id=global_id,
                        next_obs=next_obs,
                        behavior_id=self.behavior_id,
                    )
                    for traj_queue in self.trajectory_queues:
                        traj_queue.put(trajectory)
                    self.experience_buffers[global_id] = []

        # Iterate over all the terminated steps
        for terminated_step in terminal_steps.values():
            local_id = terminated_step.agent_id
            global_id = get_global_agent_id(worker_id, local_id)
            stored_decision_step, idx = self.last_step_result.get(global_id, (None, None))
            stored_take_action_outputs = self.last_take_action_outputs.get(
                global_id, None
            )
            # We do not need to store this step as this agent is terminated
            # This state is the consequence of a past action
            if stored_decision_step is not None and stored_take_action_outputs is not None:
                obs = stored_decision_step.obs
                if self.policy.use_recurrent:
                    memory = self.policy.retrieve_memories([global_id])[0, :]
                else:
                    memory = None
                done = True  #The agent is terminated
                max_step = terminated_step.max_step
                                    # Add the outputs of the last eval
                action = stored_take_action_outputs["action"][idx]
                if self.policy.use_continuous_act:
                    action_pre = stored_take_action_outputs["pre_action"][idx]
                else:
                    action_pre = None
                action_probs = stored_take_action_outputs["log_probs"][idx]
                action_mask = stored_decision_step.action_mask
                prev_action = self.policy.retrieve_previous_action([global_id])[
                    0, :
                ]
                experience = AgentExperience(
                    obs=obs,
                    reward=terminated_step.reward,
                    done=done,
                    action=action,
                    action_probs=action_probs,
                    action_pre=action_pre,
                    action_mask=action_mask,
                    prev_action=prev_action,
                    max_step=max_step,
                    memory=memory,
                )
                # Add the value outputs if needed
                self.experience_buffers[global_id].append(experience)
                self.episode_rewards[global_id] += terminated_step.reward
            
                # Since the Agent is done, we must generate the trajectory
                # Make next AgentExperience
                next_obs = terminated_step.obs
                trajectory = Trajectory(
                    steps=self.experience_buffers[global_id],
                    agent_id=global_id,
                    next_obs=next_obs,
                    behavior_id=self.behavior_id,
                )
                for traj_queue in self.trajectory_queues:
                    traj_queue.put(trajectory)
                self.experience_buffers[global_id] = []
            self._clean_agent_data(global_id)

        for _gid in action_global_agent_ids:
            # If the ID doesn't have a last step result, the agent just reset,
            # don't store the action.
            if _gid in self.last_step_result:
                if "action" in take_action_outputs:
                    self.policy.save_previous_action(
                        [_gid], take_action_outputs["action"]
                    )

    def _clean_agent_data(self, global_id: str) -> None:
        """
        Removes the data for an Agent.
        """
        self._safe_delete(self.experience_buffers, global_id)
        self._safe_delete(self.last_take_action_outputs, global_id)
        self._safe_delete(self.last_step_result, global_id)
        self._safe_delete(self.episode_steps, global_id)
        self._safe_delete(self.episode_rewards, global_id)
        self.policy.remove_previous_action([global_id])
        self.policy.remove_memories([global_id])

    def _safe_delete(self, my_dictionary: Dict[Any, Any], key: Any) -> None:
        """
        Safe removes data from a dictionary. If not found,
        don't delete.
        """
        if key in my_dictionary:
            del my_dictionary[key]

    def publish_trajectory_queue(
        self, trajectory_queue: "AgentManagerQueue[Trajectory]"
    ) -> None:
        """
        Adds a trajectory queue to the list of queues to publish to when this AgentProcessor
        assembles a Trajectory
        :param trajectory_queue: Trajectory queue to publish to.
        """
        self.trajectory_queues.append(trajectory_queue)

    def end_episode(self) -> None:
        """
        Ends the episode, terminating the current trajectory and stopping stats collection for that
        episode. Used for forceful reset (e.g. in curriculum or generalization training.)
        """
        all_gids = list(self.experience_buffers.keys())  # Need to make copy
        for _gid in all_gids:
            self._clean_agent_data(_gid)


class AgentManagerQueue(Generic[T]):
    """
    Queue used by the AgentManager. Note that we make our own class here because in most implementations
    deque is sufficient and faster. However, if we want to switch to multiprocessing, we'll need to change
    out this implementation.
    """

    class Empty(Exception):
        """
        Exception for when the queue is empty.
        """

        pass

    def __init__(self, behavior_id: str, maxlen: int = 1000):
        """
        Initializes an AgentManagerQueue. Note that we can give it a behavior_id so that it can be identified
        separately from an AgentManager.
        """
        self.maxlen: int = maxlen
        self.queue: Deque[T] = deque(maxlen=self.maxlen)
        self.behavior_id = behavior_id

    def empty(self) -> bool:
        return len(self.queue) == 0

    def get_nowait(self) -> T:
        try:
            return self.queue.popleft()
        except IndexError:
            raise self.Empty("The AgentManagerQueue is empty.")

    def put(self, item: T) -> None:
        self.queue.append(item)


class AgentManager(AgentProcessor):
    """
    An AgentManager is an AgentProcessor that also holds a single trajectory and policy queue.
    Note: this leaves room for adding AgentProcessors that publish multiple trajectory queues.
    """

    def __init__(
        self,
        policy: TFPolicy,
        behavior_id: str,
        stats_reporter: StatsReporter,
        max_trajectory_length: int = sys.maxsize,
    ):
        super().__init__(policy, behavior_id, stats_reporter, max_trajectory_length)
        self.trajectory_queue: AgentManagerQueue[Trajectory] = AgentManagerQueue(
            self.behavior_id
        )
        self.policy_queue: AgentManagerQueue[Policy] = AgentManagerQueue(
            self.behavior_id
        )
        self.publish_trajectory_queue(self.trajectory_queue)
