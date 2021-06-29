from abc import ABC, abstractmethod
from threading import Lock, Thread
from queue import Queue, LifoQueue, Empty, Full
from time import time
from overcooked_ai_py.agents.agent import Agent
from overcooked_ai_py.mdp.overcooked_mdp import OvercookedGridworld
from overcooked_ai_py.mdp.overcooked_env import OvercookedEnv
from overcooked_ai_py.mdp.actions import Action, Direction
from overcooked_ai_py.planning.planners import MotionPlanner, NO_COUNTERS_PARAMS
from ray.rllib import policy
from utils import SafeGameMethod
import random, os, pickle, json, math, traceback

# Relative path to where all static pre-trained agents are stored on server
AGENT_DIR = None

# Maximum allowable game time (in seconds)
MAX_GAME_TIME = None

# Maximum number of frames per second a game is allowed to run at
MAX_FPS = None

def _configure(max_game_time, agent_dir, max_fps):
    global AGENT_DIR, MAX_GAME_TIME, MAX_FPS
    MAX_GAME_TIME = max_game_time
    AGENT_DIR = agent_dir
    MAX_FPS = max_fps

class Game(ABC):

    """
    Class representing a game object. Coordinates the simultaneous actions of arbitrary
    number of players. Override this base class in order to use. 

    Players can post actions to a `pending_actions` queue, and driver code can call `tick` to apply these actions.


    It should be noted that most operations in this class are not on their own thread safe. Thus, client code should
    acquire `self.lock` before making any modifications to the instance. 

    One important exception to the above rule is `enqueue_actions` which is thread safe out of the box
    """

    # Possible TODO: create a static list of IDs used by the class so far to verify id uniqueness
    # This would need to be serialized, however, which might cause too great a performance hit to 
    # be worth it

    EMPTY = 'EMPTY'
    
    class Status:
        DONE = 'done'
        ACTIVE = 'active'
        RESET = 'reset'
        INACTIVE = 'inactive'
        ERROR = 'error'



    def __init__(self, *args, **kwargs):
        """
        players (list): List of IDs of players currently in the game
        spectators (set): Collection of IDs of players that are not allowed to enqueue actions but are currently watching the game
        id (int):   Unique identifier for this game
        pending_actions List[(Queue)]: Buffer of (player_id, action) pairs have submitted that haven't been commited yet
        lock (Lock):    Used to serialize updates to the game state
        is_active(bool): Whether the game is currently being played or not
        fps (int): Number of times `tick` will be called per second
        """
        self.players = []
        self.spectators = set()
        self.pending_actions = []
        self.id = kwargs.get('id', id(self))
        self.lock = Lock()
        self._is_active_ = False
        self.fps = min(kwargs.get('fps', MAX_FPS), MAX_FPS)

    @abstractmethod
    def _is_full(self):
        """
        Returns whether there is room for additional players to join or not
        """
        pass

    @abstractmethod
    def _apply_action(self, player_idx, action):
        """
        Updates the game state by applying a single (player_idx, action) tuple. Subclasses should try to override this method
        if possible
        """
        pass


    @abstractmethod
    def _is_finished(self):
        """
        Returns whether the game has concluded or not
        """
        pass

    def _is_ready(self):
        """
        Returns whether the game can be started. Defaults to having enough players
        """
        return self.is_full()

    @SafeGameMethod
    def is_full(self):
        return self._is_full()

    @SafeGameMethod
    def is_finished(self):
        return self._is_finished()

    @SafeGameMethod
    def is_ready(self):
        return self._is_ready()

    def _is_active(self):
        """
        Whether the game is currently being played
        """
        return self._is_active_

    @SafeGameMethod
    def is_active(self):
        return self._is_active()

    @property
    def reset_timeout(self):
        """
        Number of milliseconds to pause game on reset
        """
        return 3000

    def _apply_actions(self):
        """
        Updates the game state by applying each of the pending actions in the buffer. Is called by the tick method. Subclasses
        should override this method if joint actions are necessary. If actions can be serialized, overriding `apply_action` is 
        preferred
        """
        for i in range(len(self.players)):
            try:
                while True:
                    action = self.pending_actions[i].get(block=False)
                    self._apply_action(i, action)
            except Empty:
                pass
    
    def _activate(self):
        """
        Activates the game to let server know real-time updates should start. Provides little functionality but useful as
        a check for debugging
        """
        self._is_active_ = True

    def _deactivate(self):
        """
        Deactives the game such that subsequent calls to `tick` will be no-ops. Used to handle case where game ends but 
        there is still a buffer of client pings to handle
        """
        self._is_active_ = False

    @SafeGameMethod
    def activate(self):
        return self._activate()

    @SafeGameMethod
    def deactivate(self):
        return self._deactivate()

    def _reset(self):
        """
        Restarts the game while keeping all active players by resetting game stats and temporarily disabling `tick`
        """
        if not self._is_active():
            raise ValueError("Inactive Games cannot be reset")
        if self._is_finished():
            return self.Status.DONE
        self._deactivate()
        self._activate()
        return self.Status.RESET

    @SafeGameMethod
    def reset(self):
        return self._reset()

    def _needs_reset(self):
        """
        Returns whether the game should be reset on the next call to `tick`
        """
        return False


    def _tick(self):
        """
        Updates the game state by applying each of the pending actions. This is done so that players cannot directly modify
        the game state, offering an additional level of safety and thread security. 

        One can think of "enqueue_action" like calling "git add" and "tick" like calling "git commit"

        Subclasses should try to override `apply_actions` if possible. Only override this method if necessary
        """ 
        if not self._is_active():
            return self.Status.INACTIVE
        if self._needs_reset():
            self._reset()
            return self.Status.RESET

        self._apply_actions()
        return self.Status.DONE if self.is_finished() else self.Status.ACTIVE

    @SafeGameMethod
    def tick(self):
        return self._tick()
    
    def _enqueue_action(self, player_id, action):
        """
        Add (player_id, action) pair to the pending action queue, without modifying underlying game state

        Note: This function IS thread safe
        """
        if not self._is_active():
            # Could run into issues with is_active not being thread safe
            return
        if player_id not in self.players:
            # Only players actively in game are allowed to enqueue actions
            return
        try:
            player_idx = self.players.index(player_id)
            self.pending_actions[player_idx].put(action)
        except Full:
            pass
    
    @SafeGameMethod
    def enqueue_action(self, player_id, action):
        return self._enqueue_action(player_id, action)

    def _get_state(self):
        """
        Return a JSON compatible serialized state of the game. Note that this should be as minimalistic as possible
        as the size of the game state will be the most important factor in game performance. This is sent to the client
        every frame update.
        """
        return { "players" : self.players }

    @SafeGameMethod
    def get_state(self):
        return self._get_state()

    def _to_json(self):
        """
        Return a JSON compatible serialized state of the game. Contains all information about the game, does not need to
        be minimalistic. This is sent to the client only once, upon game creation
        """
        return self.get_state()

    @SafeGameMethod
    def to_json(self):
        return self._to_json()

    def _is_empty(self):
        """
        Return whether it is safe to garbage collect this game instance
        """
        return not self.num_players

    @SafeGameMethod
    def is_empty(self):
        return self._is_empty()

    def _add_player(self, player_id, idx=None, buff_size=-1):
        """
        Add player_id to the game
        """
        if self._is_full():
            raise ValueError("Cannot add players to full game")
        if self._is_active():
            raise ValueError("Cannot add players to active games")
        if not idx and self.EMPTY in self.players:
            idx = self.players.index(self.EMPTY)
        elif not idx:
            idx = len(self.players)
        
        padding = max(0, idx - len(self.players) + 1)
        for _ in range(padding):
            self.players.append(self.EMPTY)
            self.pending_actions.append(self.EMPTY)
        
        self.players[idx] = player_id
        self.pending_actions[idx] = Queue(maxsize=buff_size)

    @SafeGameMethod
    def add_player(self, player_id, idx=None, buff_size=-1):
        return self._add_player(player_id, idx, buff_size)

    def _add_spectator(self, spectator_id):
        """
        Add spectator_id to list of spectators for this game
        """
        if spectator_id in self.players:
            raise ValueError("Cannot spectate and play at same time")
        self.spectators.add(spectator_id)

    @SafeGameMethod
    def add_spectator(self, spectator_id):
        return self._add_spectator(spectator_id)

    def _remove_player(self, player_id):
        """
        Remove player_id from the game
        """
        try:
            idx = self.players.index(player_id)
            self.players[idx] = self.EMPTY
            self.pending_actions[idx] = self.EMPTY
        except ValueError:
            return False
        else:
            return True

    def _remove_spectator(self, spectator_id):
        """
        Removes spectator_id if they are in list of spectators. Returns True if spectator successfully removed, False otherwise
        """
        try:
            self.spectators.remove(spectator_id)
        except ValueError:
            return False
        else:
            return True

    @SafeGameMethod
    def remove_spectator(self, spectator_id):
        return self._remove_spectator(spectator_id)

    @SafeGameMethod
    def remove_player(self, player_id):
        return self._remove_player(player_id)


    def _clear_pending_actions(self):
        """
        Remove all queued actions for all players
        """
        for i, player in enumerate(self.players):
            if player != self.EMPTY:
                queue = self.pending_actions[i]
                queue.queue.clear()

    def _num_players(self):
        return len([player for player in self.players if player != self.EMPTY])

    @property
    @SafeGameMethod
    def num_players(self):
        return self._num_players()

    def _get_data(self):
        """
        Return any game metadata to server driver. Really only relevant for Psiturk code
        """
        return {}

    @SafeGameMethod
    def get_data(self):
        return self._get_data()
        


class DummyGame(Game):

    """
    Standin class used to test basic server logic
    """

    def __init__(self, **kwargs):
        super(DummyGame, self).__init__(**kwargs)
        self.counter = 0

    def _is_full(self):
        return self.num_players == 2

    def _apply_action(self, idx, action):
        pass

    def _apply_actions(self):
        self.counter += 1

    def _is_finished(self):
        return self.counter >= 100

    def _get_state(self):
        state = super(DummyGame, self)._get_state()
        state['count'] = self.counter
        return state


class DummyInteractiveGame(Game):

    """
    Standing class used to test interactive components of the server logic
    """

    def __init__(self, **kwargs):
        super(DummyInteractiveGame, self).__init__(**kwargs)
        self.max_players = int(kwargs.get('playerZero', 'human') == 'human') + int(kwargs.get('playerOne', 'human') == 'human')
        self.max_count = kwargs.get('max_count', 30)
        self.counter = 0
        self.counts = [0] * self.max_players

    def _is_full(self):
        return self.num_players == self.max_players

    def _is_finished(self):
        return max(self.counts) >= self.max_count

    def _apply_action(self, player_idx, action):
        if action.upper() == Direction.NORTH:
            self.counts[player_idx] += 1
        if action.upper() == Direction.SOUTH:
            self.counts[player_idx] -= 1

    def _apply_actions(self):
        super(DummyInteractiveGame, self)._apply_actions()
        self.counter += 1

    def _get_state(self):
        state = super(DummyInteractiveGame, self)._get_state()
        state['count'] = self.counter
        for i in range(self.num_players):
            state['player_{}_count'.format(i)] = self.counts[i]
        return state

class BuggyGame(DummyInteractiveGame):
    def __init__(self, *args, buggy_activate=False, buggy_tick=True, buggy_add_player=False, buggy_enqueue_action=False, **kwargs):
        super(BuggyGame, self).__init__(*args, **kwargs)
        self.buggy_activate = buggy_activate
        self.buggy_tick = buggy_tick
        self.buggy_add_player = buggy_add_player
        self.buggy_enqueue_action = buggy_enqueue_action


    def _activate(self):
        super(BuggyGame, self)._activate()
        if self.buggy_activate:
            raise Exception("This is a bug!")

    def _tick(self):
        super(BuggyGame, self)._tick()
        if self.buggy_tick:
            raise Exception("This is a bug!")

    def _add_player(self, *args, **kwargs):
        super(BuggyGame, self)._add_player(*args, **kwargs)
        if self.buggy_add_player:
            raise Exception("This is a bug!")

    def _enqueue_action(self, *args, **kwargs):
        super(BuggyGame, self)._enqueue_action(*args, **kwargs)
        if self.buggy_enqueue_action:
            raise Exception("This is a bug!")
    
class OvercookedGame(Game):
    """
    Class for bridging the gap between Overcooked_Env and the Game interface

    Instance variable:
        - max_players (int): Maximum number of players that can be in the game at once
        - mdp (OvercookedGridworld): Controls the underlying Overcooked game logic
        - score (int): Current reward acheived by all players
        - max_time (int): Number of seconds the game should last
        - npc_policies (dict): Maps user_id to policy (Agent) for each AI player
        - npc_state_queues (dict): Mapping of NPC user_ids to LIFO queues for the policy to process
        - curr_tick (int): How many times the game server has called this instance's `tick` method
        - ticker_per_ai_action (int): How many frames should pass in between NPC policy forward passes. 
            Note that this is a lower bound; if the policy is computationally expensive the actual frames
            per forward pass can be higher
        - action_to_overcooked_action (dict): Maps action names returned by client to action names used by OvercookedGridworld
            Note that this is an instance variable and not a static variable for efficiency reasons
        - human_players (set(str)): Collection of all player IDs that correspond to humans
        - npc_players (set(str)): Collection of all player IDs that correspond to AI
        - randomized (boolean): Whether the order of the layouts should be randomized
    
    Methods:
        - npc_policy_consumer: Background process that asynchronously computes NPC policy forward passes. One thread
            spawned for each NPC
        - _curr_game_over: Determines whether the game on the current mdp has ended
    """

    def __init__(self, layouts=["cramped_room"], mdp_params={}, num_players=2, gameTime=30, playerZero='human', playerOne='human', showPotential=False, randomized=False, ticks_per_ai_action=1, block_for_ai=True, **kwargs):
        super(OvercookedGame, self).__init__(**kwargs)
        self.show_potential = showPotential
        self.mdp_params = mdp_params
        self.layouts = layouts
        self.max_players = int(num_players)
        self.mdp = None
        self.mp = None
        self.score = 0
        self.phi = 0
        self.max_time = min(int(gameTime), MAX_GAME_TIME)
        self.npc_policies = {}
        self.npc_state_queues = {}
        self.action_to_overcooked_action = {
            "STAY" : Action.STAY,
            "UP" : Direction.NORTH,
            "DOWN" : Direction.SOUTH,
            "LEFT" : Direction.WEST,
            "RIGHT" : Direction.EAST,
            "SPACE" : Action.INTERACT
        }
        self.ticks_per_ai_action = ticks_per_ai_action
        self.block_for_ai = block_for_ai
        self.curr_tick = 0
        self.human_players = set()
        self.npc_players = set()

        if randomized:
            random.shuffle(self.layouts)

        if playerZero != 'human':
            player_zero_id = playerZero + '_0'
            self._add_player(player_zero_id, idx=0, buff_size=1, is_human=False)

        if playerOne != 'human':
            player_zero_id = playerOne + '_1'
            self._add_player(player_zero_id, idx=1, buff_size=1, is_human=False)
        

    def _curr_game_over(self):
        return time() - self.start_time >= self.max_time


    def _needs_reset(self):
        return self._curr_game_over() and not self._is_finished()

    def _add_player(self, player_id, idx=None, buff_size=-1, is_human=True):
        super(OvercookedGame, self)._add_player(player_id, idx=idx, buff_size=buff_size)
        if is_human:
            self.human_players.add(player_id)
        else:
            self.npc_players.add(player_id)

    def _remove_player(self, player_id):
        removed = super(OvercookedGame, self)._remove_player(player_id)
        if removed:
            if player_id in self.human_players:
                self.human_players.remove(player_id)
            elif player_id in self.npc_players:
                self.npc_players.remove(player_id)
            else:
                raise ValueError("Inconsistent state")


    def _npc_policy_consumer(self, policy_id):
        queue = self.npc_state_queues[policy_id]
        policy = self.npc_policies[policy_id]
        while self._is_active():
            state = queue.get()
            npc_action, _ = policy.action(state)
            super(OvercookedGame, self)._enqueue_action(policy_id, npc_action)


    def _is_full(self):
        return self.num_players >= self.max_players

    def _is_finished(self):
        return not self.layouts and self._curr_game_over()

    def _is_empty(self):
        """
        Game is considered safe to scrap if there are no active players or if there are no humans (spectating or playing)
        """
        return super(OvercookedGame, self)._is_empty() or not self.spectators and not self.human_players

    def _is_ready(self):
        """
        Game is ready to be activated if there are a sufficient number of players and at least one human (spectator or player)
        """
        return super(OvercookedGame, self)._is_ready() and not self._is_empty()

    def _apply_action(self, player_id, action):
        pass

    def _apply_actions(self):
        # Default joint action, as NPC policies and clients probably don't enqueue actions fast 
        # enough to produce one at every tick
        joint_action = [Action.STAY] * len(self.players)

        # Synchronize individual player actions into a joint-action as required by overcooked logic
        for i in range(len(self.players)):
            try:
                block = self.is_npc(player_idx=i) and self.block_for_ai
                joint_action[i] = self.pending_actions[i].get(block=block)
            except Empty:
                pass
        
        # Apply overcooked game logic to get state transition
        prev_state = self.state
        self.state, info = self.mdp.get_state_transition(prev_state, joint_action)
        if self.show_potential:
            self.phi = self.mdp.potential_function(prev_state, self.mp, gamma=0.99)

        # Send next state to all background consumers if needed
        if self.curr_tick % self.ticks_per_ai_action == 0:
            for npc_id in self.npc_policies:
                self.npc_state_queues[npc_id].put(self.state, block=False)

        # Update score based on soup deliveries that might have occured
        curr_reward = sum(info['sparse_reward_by_agent'])
        self.score += curr_reward

        # Return about the current transition
        return prev_state, joint_action, info
        

    def _enqueue_action(self, player_id, action):
        overcooked_action = self.action_to_overcooked_action[action]
        super(OvercookedGame, self)._enqueue_action(player_id, overcooked_action)

    def _reset(self):
        status = super(OvercookedGame, self)._reset()
        if status == self.Status.RESET:
            # Hacky way of making sure game timer doesn't "start" until after reset timeout has passed
            self.start_time += self.reset_timeout / 1000


    def _tick(self):
        self.curr_tick += 1
        return super(OvercookedGame, self)._tick()

    def _activate(self):
        super(OvercookedGame, self)._activate()

        # Sanity check at start of each game
        if not self.npc_players.union(self.human_players) == set(self.players):
            raise ValueError("Inconsistent State")

        self.curr_layout = self.layouts.pop()
        self.mdp = OvercookedGridworld.from_layout_name(self.curr_layout, **self.mdp_params)
        if self.show_potential:
            self.mp = MotionPlanner.from_pickle_or_compute(self.mdp, counter_goals=[])
        self.state = self.mdp.get_standard_start_state()
        if self.show_potential:
            self.phi = self.mdp.potential_function(self.state, self.mp, gamma=0.99)

        # Load any NPC policies, if necessary
        for npc_id in self.npc_players:
            npc_idx = self.players.index(npc_id)
            policy_id = '_'.join(npc_id.split('_')[:-1])
            self.npc_policies[npc_id] = self._get_policy(policy_id, idx=npc_idx)
            self.npc_state_queues[npc_id] = LifoQueue()

        self.start_time = time()
        self.curr_tick = 0
        self.score = 0
        self.threads = []
        for npc_policy in self.npc_policies:
            t = Thread(target=self._npc_policy_consumer, args=(npc_policy,))
            self.threads.append(t)
            t.start()
            self.npc_policies[npc_policy].reset()
            self.npc_state_queues[npc_policy].put(self.state)
            
            

    def _deactivate(self):
        super(OvercookedGame, self)._deactivate()
        # Ensure the background consumers do not hang
        for npc_policy in self.npc_policies:
            self.npc_state_queues[npc_policy].put(self.state)

        # Wait for all background threads to exit
        for t in self.threads:
            t.join()

        # Clear all action queues
        self._clear_pending_actions()


    def _get_state(self):
        state_dict = {}
        state_dict['ood'] = self.mdp.is_off_distribution(self.state) if self.show_potential else None
        state_dict['potential'] = self.phi if self.show_potential else None
        state_dict['state'] = self.state.to_dict()
        state_dict['score'] = self.score
        state_dict['time_left'] = max(self.max_time - (time() - self.start_time), 0)
        return state_dict

    def _to_json(self):
        obj_dict = {}
        obj_dict['terrain'] = self.mdp.terrain_mtx if self._is_active() else None
        obj_dict['state'] = self.get_state() if self._is_active() else None
        return obj_dict

    def _get_policy(self, npc_id, idx=0):
        if npc_id.lower().startswith("ppo"):
            import ray
            try:
                # Loading rllib agents requires additional helpers
                from human_aware_rl.rllib.rllib import PPOAgent
                fpath = os.path.join(AGENT_DIR, self.curr_layout, npc_id)
                agent = PPOAgent.load(fpath)
                agent.set_agent_index(idx)
                agent.stochastic = True
                return agent
            except Exception as e:
                print(traceback.format_exc(), flush=True)
                raise IOError("Error loading Rllib Agent\n{}".format(e.__repr__()))
            finally:
                # Always kill ray after loading agent, otherwise, ray will crash once process exits
                if ray.is_initialized():
                    ray.shutdown()
        elif npc_id.lower().startswith('bc'):
            try:
                # Loading BC agents requires additional helpers
                from human_aware_rl.imitation.behavior_cloning_tf2 import BehaviorCloningAgent
                agent_dir = os.path.join(AGENT_DIR, self.curr_layout, npc_id)
                agent = BehaviorCloningAgent.load(agent_dir)
                agent.set_agent_index(idx)
                return agent
            except Exception as e:
                print(traceback.format_exc(), flush=True)
                raise IOError("Error loading BC agent\n{}".format(e.__repr__()))

        else:
            try:
                # Loading vanilla OvercookedAgent
                agent_dir = os.path.join(AGENT_DIR, self.curr_layout, npc_id)
                agent = Agent.load(agent_dir)
                agent.set_agent_index(idx)
                return agent
            except Exception as e:
                print(traceback.format_exc(), flush=True)
                raise IOError("Error loading agent\n{}".format(e.__repr__()))

    def is_npc(self, player_id=None, player_idx=None):
        if player_id is None and player_idx is None:
            raise ValueError("Must provide either player id or index")
        if (player_id is not None) and (player_idx is not None):
            raise ValueError("Must provide iether player id or index, not both")
        if player_idx is not None:
            player_id = self.players[player_idx]
        return player_id in self.npc_players

class OvercookedPsiturk(OvercookedGame):
    """
    Wrapper on OvercookedGame that handles additional housekeeping for Psiturk experiments

    Instance Variables:
        - trajectory (list(dict)): list of state-action pairs in current trajectory
        - psiturk_uid (string): Unique id for each psiturk game instance (provided by Psiturk backend)
            Note, this is not the user id -- two users in the same game will have the same psiturk_uid
        - trial_id (string): Unique identifier for each psiturk trial, updated on each call to reset
            Note, one OvercookedPsiturk game handles multiple layouts. This is how we differentiate

    Methods:
        get_data: Returns the accumulated trajectory data and clears the self.trajectory instance variable
    
    """

    def __init__(self, *args, psiturk_uid='-1', **kwargs):
        super(OvercookedPsiturk, self).__init__(*args, showPotential=False, **kwargs)
        self.psiturk_uid = str(psiturk_uid)
        self.trajectory = []

    def _activate(self):
        """
        Resets trial ID at start of new "game"
        """
        super(OvercookedPsiturk, self)._activate()
        self.trial_id = self.psiturk_uid + str(self.start_time)

    def _apply_actions(self):
        """
        Applies pending actions then logs transition data
        """
        # Apply MDP logic
        prev_state, joint_action, info = super(OvercookedPsiturk, self)._apply_actions()

        # Log data to send to psiturk client
        curr_reward = sum(info['sparse_reward_by_agent'])
        transition = {
            "state" : json.dumps(prev_state.to_dict()),
            "joint_action" : json.dumps(joint_action),
            "reward" : curr_reward,
            "time_left" : max(self.max_time - (time() - self.start_time), 0),
            "score" : self.score,
            "time_elapsed" : time() - self.start_time,
            "cur_gameloop" : self.curr_tick,
            "layout" : json.dumps(self.mdp.terrain_mtx),
            "layout_name" : self.curr_layout,
            "trial_id" : self.trial_id,
            "player_0_id" : self.players[0],
            "player_1_id" : self.players[1],
            "player_0_is_human" : self.players[0] in self.human_players,
            "player_1_is_human" : self.players[1] in self.human_players
        }

        self.trajectory.append(transition)

    def _get_data(self):
        """
        Returns and then clears the accumulated trajectory
        """
        data = { "uid" : self.psiturk_uid  + "_" + str(time()), "trajectory" : self.trajectory }
        self.trajectory = []
        return data


class OvercookedTutorial(OvercookedGame):

    """
    Wrapper on OvercookedGame that includes additional data for tutorial mechanics, most notably the introduction of tutorial "phases"

    Instance Variables:
        - curr_phase (int): Indicates what tutorial phase we are currently on
        - phase_two_score (float): The exact sparse reward the user must obtain to advance past phase 2
        - phase_one_cook_time (int): Number of timesteps required to cook soup in first phase
    """

    # Lis of all currently supported tutorial layouts and what phase they correspond to
    LAYOUT_TO_PHASE = {
        'tutorial_0' : 0,
        'tutorial_1' : 1,
        'tutorial_2' : 2,
        'tutorial_3' : 3
    }
    

    def __init__(self, layouts=["tutorial_0"], mdp_params={}, playerZero='human', playerOne='AI', phaseTwoScore=15, phaseOneCookTime=45, **kwargs):
        if not set(layouts).issubset(self.LAYOUT_TO_PHASE):
            raise ValueError("One or more layouts is not currently supported as a valid tutorial layout!")
        self.phase_two_score = phaseTwoScore
        self.phase_one_cook_time = phaseOneCookTime
        self.phase_two_finished = False
        super(OvercookedTutorial, self).__init__(layouts=layouts, mdp_params=mdp_params, playerZero=playerZero, playerOne=playerOne, **kwargs)
        self.show_potential = False
        self.max_time = 0
        self.max_players = 2
        self.curr_phase = -1

    @property
    def reset_timeout(self):
        return 1

    def _curr_game_over(self):
        if self.curr_phase == 0:
            return self.score > 0
        elif self.curr_phase == 1:
            return self.score > 0
        elif self.curr_phase == 2:
            return self.phase_two_finished
        elif self.curr_phase == 3:
            return self.score >= float('inf')
        return False

    def _activate(self):
        super(OvercookedTutorial, self)._activate()
        self.curr_phase = self.LAYOUT_TO_PHASE[self.curr_layout]

    def _get_policy(self, *args, **kwargs):
        return TutorialAI(self.LAYOUT_TO_PHASE, self.layouts, self.ticks_per_ai_action, self.phase_one_cook_time)

    def _apply_actions(self):
        """
        Apply regular MDP logic with retroactive score adjustment tutorial purposes
        """
        prev_state, joint_action, info = super(OvercookedTutorial, self)._apply_actions()

        human_reward, ai_reward = info['sparse_reward_by_agent']

        # We only want to keep track of the human's score in the tutorial
        self.score -= ai_reward

        # Phase two requires a specific reward to complete
        if self.curr_phase == 2:
            self.score = 0
            if human_reward == self.phase_two_score:
                self.phase_two_finished = True

        return prev_state, joint_action, human_reward



class OvercookedTutorialPsiturk(OvercookedTutorial):


    def __init__(self, *args, psiturk_uid='-1', **kwargs):
        super(OvercookedTutorialPsiturk, self).__init__(*args, **kwargs)
        self.psiturk_uid = str(psiturk_uid)
        self.trajectory = []

    def _activate(self):
        """
        Resets trial ID at start of new "game"
        """
        super(OvercookedTutorialPsiturk, self)._activate()
        self.trial_id = self.psiturk_uid + '_tutorial_' + str(self.start_time)

    def _apply_actions(self):
        """
        Applies pending actions then logs transition data
        """
        # Apply MDP logic
        prev_state, joint_action, human_reward = super(OvercookedTutorialPsiturk, self)._apply_actions()

        # Log data to send to psiturk client
        transition = {
            "state" : json.dumps(prev_state.to_dict()),
            "joint_action" : json.dumps(joint_action),
            "reward" : human_reward,
            "time_left" : max(self.max_time - (time() - self.start_time), 0),
            "score" : self.score,
            "time_elapsed" : time() - self.start_time,
            "cur_gameloop" : self.curr_tick,
            "layout" : json.dumps(self.mdp.terrain_mtx),
            "layout_name" : self.curr_layout,
            "trial_id" : self.trial_id,
            "player_0_id" : self.players[0],
            "player_1_id" : self.players[1],
            "player_0_is_human" : self.players[0] in self.human_players,
            "player_1_is_human" : self.players[1] in self.human_players
        }

        self.trajectory.append(transition)

    def _get_data(self):
        """
        Returns and then clears the accumulated trajectory
        """
        data = { "uid" : self.psiturk_uid  + "_" + str(time()), "tutorial_trajectory" : self.trajectory }
        self.trajectory = []
        return data





class DummyOvercookedGame(OvercookedGame):
    """
    Class that hardcodes the AI to be random. Used for debugging
    """
    
    def __init__(self, layouts=["cramped_room"], **kwargs):
        super(DummyOvercookedGame, self).__init__(layouts, **kwargs)

    def get_policy(self, *args, **kwargs):
        return DummyAI()


class DummyAI():
    """
    Randomly samples actions. Used for debugging
    """
    def action(self, state):
        [action] = random.sample([Action.STAY, Direction.NORTH, Direction.SOUTH, Direction.WEST, Direction.EAST, Action.INTERACT], 1)
        return action, None

    def reset(self):
        pass

class DummyComputeAI(DummyAI):
    """
    Performs simulated compute before randomly sampling actions. Used for debugging
    """
    def __init__(self, compute_unit_iters=1e5):
        """
        compute_unit_iters (int): Number of for loop cycles in one "unit" of compute. Number of 
                                    units performed each time is randomly sampled
        """
        super(DummyComputeAI, self).__init__()
        self.compute_unit_iters = int(compute_unit_iters)
    
    def action(self, state):
        # Randomly sample amount of time to busy wait
        iters = random.randint(1, 10) * self.compute_unit_iters

        # Actually compute something (can't sleep) to avoid scheduling optimizations
        val = 0
        for i in range(iters):
            # Avoid branch prediction optimizations
            if i % 2 == 0:
                val += 1
            else:
                val += 2
        
        # Return randomly sampled action
        return super(DummyComputeAI, self).action(state)

    
class StayAI():
    """
    Always returns "stay" action. Used for debugging
    """
    def action(self, state):
        return Action.STAY, None

    def reset(self):
        pass


class TutorialAI():

    COOK_SOUP_ACTIONS = [
        # Grab first onion
        Direction.WEST,
        Direction.WEST,
        Direction.WEST,
        Action.INTERACT,

        # Place onion in pot
        Direction.EAST,
        Direction.NORTH,
        Action.INTERACT,

        # Grab second onion
        Direction.WEST,
        Action.INTERACT,

        # Place onion in pot
        Direction.EAST,
        Direction.NORTH,
        Action.INTERACT,

        # Grab third onion
        Direction.WEST,
        Action.INTERACT,

        # Place onion in pot
        Direction.EAST,
        Direction.NORTH,
        Action.INTERACT,

        # Cook soup
        Action.INTERACT
    ]

    GRAB_PLATE_ACTIONS = [
        # Grab plate
        Direction.EAST,
        Direction.SOUTH,
        Action.INTERACT,
        Direction.WEST,
        Direction.NORTH,
    ]

    DELIVER_SOUP_ACTIONS = [
        # Deliver soup
        Action.INTERACT,
        Direction.EAST,
        Direction.EAST,
        Direction.EAST,
        Action.INTERACT,
        Direction.WEST
    ]

    COOK_SOUP_COOP_LOOP = [
        # Grab first onion
        Direction.WEST,
        Direction.WEST,
        Direction.WEST,
        Action.INTERACT,

        # Place onion in pot
        Direction.EAST,
        Direction.SOUTH,
        Action.INTERACT,

        # Move to start so this loops
        Direction.EAST,
        Direction.EAST,

        # Pause to make cooperation more real time
        Action.STAY,
        Action.STAY,
        Action.STAY,
        Action.STAY,
        Action.STAY,
        Action.STAY,
        Action.STAY,
        Action.STAY,
        Action.STAY
    ]

    def __init__(self, layout_to_phase_map, layouts=['tutorial_0'], ticks_per_action=8, soup_cook_time=45):
        if ticks_per_action <= 0 or soup_cook_time <= 0:
            raise ValueError("Ticks per action and soup cook time must both be >= 0!")
        self.layout_to_phase = layout_to_phase_map
        self.layouts = layouts.copy()
        self.curr_layout = None
        self.curr_phase = -1
        self.curr_tick = -1
        self._build_cooking_loop(ticks_per_action, soup_cook_time)

        
    def _build_cooking_loop(self, ticks_per_action, soup_cook_time):
        # Calculate number of "STAY" actions necessary to wait for soup to cook
        grab_plate_ticks = 2 * (ticks_per_action - 1) + len(self.GRAB_PLATE_ACTIONS) * ticks_per_action
        cook_ticks_remaining = max(0, soup_cook_time - grab_plate_ticks)
        num_noops = math.ceil(cook_ticks_remaining / ticks_per_action)

        # Concatenate all Cooking routines
        self.WAIT_TO_COOK_ACTIONS = [Action.STAY] * num_noops
        self.COOK_SOUP_LOOP = [*self.COOK_SOUP_ACTIONS, *self.GRAB_PLATE_ACTIONS, *self.WAIT_TO_COOK_ACTIONS, *self.DELIVER_SOUP_ACTIONS]

    def action(self, state):
        self.curr_tick += 1
        if self.curr_phase == 0:
            return self.COOK_SOUP_LOOP[self.curr_tick % len(self.COOK_SOUP_LOOP)], None
        elif self.curr_phase == 2:
            return self.COOK_SOUP_COOP_LOOP[self.curr_tick % len(self.COOK_SOUP_COOP_LOOP)], None
        return Action.STAY, None

    def reset(self):
        self.curr_layout = self.layouts.pop()
        self.curr_phase = self.layout_to_phase[self.curr_layout]
        self.curr_tick = -1

    