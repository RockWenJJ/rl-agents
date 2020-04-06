import operator
from collections import OrderedDict, defaultdict

import numpy as np
import logging
from rl_agents.agents.common.factory import safe_deepcopy_env
from rl_agents.agents.tree_search.graph_based import GraphBasedPlannerAgent, GraphNode, GraphBasedPlanner
from rl_agents.agents.tree_search.olop import OLOP
from rl_agents.utils import kl_upper_bound, max_expectation_under_constraint

logger = logging.getLogger(__name__)


class GraphDecisionNode(GraphNode):
    """
        Decision nodes have different meanings depending on their location:
            - planner.nodes[s] stores a DecisionsNode holding information about s: N(s), V(s)
            - DecisionNode.transition[a] stores a ChanceNode holding information about (s,a): N(s,a), Q(s,a), p(s'|s,a)
            - ChanceNode.children[s'] stores a DecisionNode holding information about (s,a,s'): N(s,a,s'), R(s,a,s')


                                              planner.nodes
                                                    |
                                              DecisionNode(s)
                                                 |     |
                                        ActionNode(s,a) ...
                                          |      |
                                  DecisonNode(s,a,s')   ...
    """
    def __init__(self, planner, state, observation):
        super().__init__(planner, state, observation)
        self.count = 0
        """ Visit count N(s) (when in planner.nodes) or N(s,a,s') (when child of a chance node)"""
        self.cumulative_reward = 0
        """ Sum of all rewards r(s,a,s') (when child of a chance node). """
        self.mu_ucb = 1
        """ Upper bound on mean r(s,a,s') (when child of a chance node). """
        self.mu_lcb = 0
        """ Lower bound on mean r(s,a,s') (when child of a chance node)"""

    def sampling_rule(self):
        """
            Optimistic action sampling
        """
        if not self.children:
            self.expand()
        q_values_upper = self.backup("value_upper")
        actions = list(q_values_upper.keys())
        index = self.random_argmax(list(q_values_upper.values()))
        return actions[index]

    def selection_rule(self):
        """
            Conservative action selection
        """
        q_values_lower = self.backup("value_lower")
        actions = list(q_values_lower.keys())
        index = self.random_argmax(list(q_values_lower.values()))
        return actions[index]

    def update(self, reward=None):
        self.count += 1
        if reward is not None:
            self.cumulative_reward += reward
            self.compute_reward_ucb()

    def compute_reward_ucb(self):
        if self.planner.config["upper_bound"]["type"] == "kullback-leibler":
            # Variables available for threshold evaluation
            horizon = self.planner.config["horizon"]
            actions = self.planner.env.action_space.n
            count = self.count
            time = self.planner.config["episodes"]
            threshold = eval(self.planner.config["upper_bound"]["threshold"])
            if threshold == 0:
                self.mu_ucb = self.mu_lcb = self.cumulative_reward / self.count
            else:
                self.mu_ucb = kl_upper_bound(self.cumulative_reward, self.count, 0,
                                             threshold=str(threshold))
                self.mu_lcb = kl_upper_bound(self.cumulative_reward, self.count, 0,
                                             threshold=str(threshold), lower=True)
        else:
            logger.error("Unknown upper-bound type")

    def backup(self, field):
        return {action: chance_node.backup(field) for action, chance_node in self.children.items()}

    def partial_value_iteration(self, queue=None):
        if queue is None:
            queue = [self]
        while queue:
            node = queue.pop(0)
            delta = 0
            for field in ["value_lower", "value_upper"]:
                action_value = node.backup(field)  # Q(s, a)
                state_value_bound = np.amax(list(action_value.values()))
                delta = max(delta, abs(getattr(node, field) - state_value_bound))
                setattr(node, field, state_value_bound)
            if delta > self.planner.config["accuracy"]:
                queue.extend(list(node.parents))

    def expand(self):
        for action in self.actions_list():
            self.children[action] = GraphChanceNode(self.planner, parent=self)

    def actions_list(self):
        if self.state is None:
            raise Exception("The state should be set before expanding a node")
        try:
            return self.state.get_available_actions()
        except AttributeError:
            return range(self.state.action_space.n)

    def get_child(self, action):
        return self.children[action]

    def get_field(self, field):
        """ In case this nodes encodes the transition (s,a,s'), return the estimate of the s' state representative."""
        if str(self.observation) in self.planner.nodes:
            representative = self.planner.nodes[str(self.observation)]
            return getattr(representative, field)
        else:
            return getattr(self, field)

    def __str__(self):
        return "{} (L:{:.2f}, U:{:.2f})".format(str(self.observation), self.value_lower, self.value_upper)

    def __repr__(self):
        return self.__str__()


class GraphChanceNode(GraphNode):
    """
        Chance nodes stores the next states of a transition
    """
    def __init__(self, planner, parent):
        super().__init__(planner, state=None, observation=None)
        self.parent = parent
        self.count = 0
        """ Visit count N(s, a) (when in planner.nodes) or N(s,a,s') (when child of a chance node)"""

        self.p_hat, self.p_plus, self.p_minus = None, None, None

        # Generate placeholder nodes
        self.children = OrderedDict()
        for i in range(self.planner.config["max_next_states_count"]):
            self.children["placeholder_{}".format(i)] = GraphDecisionNode(self.planner,
                                                                          state=None,
                                                                          observation="placeholder")

    def selection_rule(self):
        """
            Sample state under the conservative distribution
        """
        return self.planner.np_random.choice(list(self.children.keys()), p=self.p_minus)

    def sampling_rule(self):
        """
            Sample state under the conservative distribution
        """
        return self.planner.np_random.choice(self.children, p=self.p_plus)

    def update(self):
        self.count += 1

    def backup(self, field):
        """
            Bellman Q(s,a) = r(s,a) + gamma E_s' V(s')
        """
        if self.count == 0:
            return self.value_upper if field == "value_upper" else self.value_lower

        gamma = self.planner.config["gamma"]
        self.p_hat = np.array([child.count for child in self.children.values()]) / self.count
        threshold = self.transition_threshold() / self.count

        if field == "value_upper":
            u_next = np.array([c.mu_ucb + gamma * c.get_field(field) for c in self.children.values()])
            self.p_plus = max_expectation_under_constraint(u_next, self.p_hat, threshold)
            self.value_upper = self.p_plus @ u_next
            return self.value_upper
        elif field == "value_lower":
            l_next = np.array([c.mu_lcb + gamma * c.get_field(field) for c in self.children.values()])
            self.p_minus = max_expectation_under_constraint(-l_next, self.p_hat, threshold)
            self.value_lower = self.p_minus @ l_next
            return self.value_lower

    def transition_threshold(self):
        horizon = self.planner.config["horizon"]
        actions = self.planner.env.action_space.n
        count = self.count
        time = self.planner.config["episodes"]
        return eval(self.planner.config["upper_bound"]["transition_threshold"])

    def get_child(self, observation):
        if str(observation) not in self.children:
            # Assign the first available placeholder to the observation
            for i in range(self.planner.config["max_next_states_count"]):
                if "placeholder_{}".format(i) in self.children:
                    self.children[str(observation)] = self.children.pop("placeholder_{}".format(i))
                    self.children[str(observation)].observation = observation
                    self.planner.get_node(observation).parents.add(self.parent)
                    break
            else:
                raise ValueError("No more placeholder nodes available, we observed more next states than "
                                 "the 'max_next_states_count' config")
        return self.children[str(observation)]

    def __str__(self):
        return "{} (L:{:.2f}, U:{:.2f})".format(id(self), self.value_lower, self.value_upper)

    def __repr__(self):
        return self.__str__()


class StochasticGraphBasedPlanner(GraphBasedPlanner):
    NODE_TYPE = GraphDecisionNode

    def __init__(self, env, config=None):
        super().__init__(env, config)

    def run(self, state, observation):
        """
        :param state: the initial environment state
        """
        # We need randomness
        state.seed(self.np_random.randint(2**30))
        if self.root.children:
            logger.debug(" / ".join(["a{} ({}): [{:.3f}, {:.3f}]".format(k, n.count, n.value_lower, n.value_upper)
                                     for k, n in self.root.children.items()]))
        update_queue = []
        # Follow sampling rule, expand graph if needed, collect rewards and update confidence bounds.
        for h in range(self.config["horizon"]):
            decision_node = self.get_node(observation, state)
            action = decision_node.sampling_rule()
            chance_node = decision_node.get_child(action)

            # Perform transition
            observation, reward, done, _ = self.step(state, action)
            next_decision_node = chance_node.get_child(observation)

            # Update local statistics
            decision_node.update()
            chance_node.update()
            next_decision_node.update(reward)

            # Track updated nodes
            if decision_node not in update_queue:
                update_queue.append(decision_node)

        # Value iteration
        decision_node.partial_value_iteration(queue=list(reversed(update_queue)))

    def plan(self, state, observation):
        self.root = self.get_node(observation, state=state)
        for _ in np.arange(self.config["episodes"]):
            self.run(safe_deepcopy_env(state), observation)

        return self.get_plan()

    def reset(self):
        self.root = self.NODE_TYPE(self, None, None)
        if "horizon" not in self.config:
            budget = max(self.env.action_space.n, self.config["budget"])
            self.config["episodes"], self.config["horizon"] = OLOP.allocation(budget, self.config["gamma"])


class StochasticGraphBasedPlannerAgent(GraphBasedPlannerAgent):
    PLANNER_TYPE = StochasticGraphBasedPlanner

    @classmethod
    def default_config(cls):
        cfg = super().default_config()
        cfg.update({
            "max_next_states_count": 1,
            "upper_bound": {
                    "type": "kullback-leibler",
                    "time": "global",
                    "threshold": "1*np.log(time)",
                    "transition_threshold": "0.1*np.log(time)"
                },
        })
        return cfg
