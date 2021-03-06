import sys
import copy
import math
import random
import numpy as np
from phase_gru import PGRU
from gru import GRU
import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable
from itertools import product
from PyQt4 import QtGui, QtCore
from visualization import QTVisualizer, q_refresh

dtype = torch.cuda.FloatTensor if torch.cuda.is_available() else torch.FloatTensor

def create_obstacles(width, height):
	return [(3,3),(6,3),(3,6),(6,6)] # 12 x 12

def obstacle_movement(t):
	if t % 6 == 0:
		return (0,1) # move up
	elif t % 6 == 1:
		return (1,0) # move right
	elif t % 6 == 2:
		return (1,0) # move right
	elif t % 6 == 3:
		return (0,-1) # move down
	elif t % 6 == 4:
		return (-1,0) # move left
	elif t % 6 == 5:
		return (-1,0) # move left

def create_targets(memory, q_vals, target_net, policy_type, gamma=1):
	# memory: 0 - current_state 1: action index 2: reward 3: next state
	n_eps = len(memory)
	action_space_size = target_net.output_size 
	q_target = torch.zeros((n_eps, action_space_size))
	
	for i in range(n_eps):
		phase_prime = memory[i][5]
		if policy_type == 0:
			s_prime = Variable(torch.from_numpy(np.array(memory[i][3].state)).type(dtype), requires_grad=False).unsqueeze(0)
			q_prime = target_net.forward(s_prime)
		elif policy_type == 1:
			inp = np.concatenate((np.array(memory[i][3].state), np.asarray([phase_prime])))
			s_prime = Variable(torch.from_numpy(inp).type(dtype), requires_grad=False).unsqueeze(0)
			q_prime = target_net.forward(s_prime)
		elif policy_type == 2:
			s_prime = Variable(torch.from_numpy(np.array(memory[i][3].state)).type(dtype), requires_grad=False).unsqueeze(0)
			q_prime = target_net.forward(s_prime, phase_prime)


		q_target[i,:] = q_vals[i][0,:].data.clone()
		q_target[i, memory[i][1]] = gamma*(memory[i][2] + q_prime.data[0,np.argmax(q_prime.data.cpu().numpy())])

	target_net.reset()
	return q_target


def goal_1_reward_func(w,t,p):
	#return -20*math.sin(w*t + p) + 5
	return -20

def goal_2_reward_func(w,t,p):
	#return 20*math.sin(w*t + p) + 5
	return 20


class State():
	def __init__(self, coordinates, list_of_obstacles):
		#coordinates - tuple, list_of_obstacles - list of tuples
		assert(len(coordinates) == 2)
		self.coordinates = coordinates
		self.n_obs = 0
		for obs in list_of_obstacles:
			assert(len(obs) == 2)
			self.n_obs += 1
		
		self.list_of_obstacles = list_of_obstacles
		self.state = np.zeros(2*(self.n_obs+1))
		self.state[0] = self.coordinates[0]
		self.state[1] = self.coordinates[1]
		for i in range(1,len(list_of_obstacles)+1):
			self.state[2*i] = list_of_obstacles[i-1][0]
			self.state[2*i+1] = list_of_obstacles[i-1][1]
		

class Action():
	def __init__(self, delta):
		#delta - number (integer)
		assert(delta in (0,1,2,3,4))
		self.delta = delta

	@staticmethod
	def oned_to_twod(delta):
		assert(delta in (0,1,2,3,4))
		if delta == 0:
			return (0,0) # no movement
		elif delta == 1:
			return (0,1) # up
		elif delta == 2:
			return (0,-1) # down
		elif delta == 3:
			return (-1,0) # left
		elif delta == 4:
			return (1,0) # right



class RewardFunction():
	def __init__(self, penalty, goal_1_coordinates, goal_1_func, goal_2_coordinates, goal_2_func, w1=None, w2=None):
		# penalty - number (integer), goal_1_coordinates - tuple, goal_1_func - lambda func returning number, goal_2_coordinates - tuple, goal_2_func - lambda function returning number
		self.terminal = False
		self.penalty = penalty
		self.goal_1_func = goal_1_func
		self.goal_2_func = goal_2_func
		self.goal_1_coordinates = goal_1_coordinates
		self.goal_2_coordinates = goal_2_coordinates
		self.t = 0 # timer
		self.w1 = w1
		self.w2 = w2
		self.p = None
		

	def __call__(self, state, action, state_prime):
		self.t += 1
		if state_prime.coordinates != self.goal_1_coordinates and state_prime.coordinates != self.goal_2_coordinates:
			return self.penalty

		if state_prime.coordinates == self.goal_1_coordinates:
			self.terminal = True
			return self.goal_1_func(self.w1, self.t, self.p)

		if state_prime.coordinates == self.goal_2_coordinates:
			self.terminal = True
			return self.goal_2_func(self.w2, self.t, self.p)

	def reset(self, goal_1_func=None, goal_2_func=None):
		self.terminal = False
		self.t = 0

		if goal_1_func != None:
			self.goal_1_func = goal_1_func
		if goal_2_func != None:
			self.goal_2_func = goal_2_func


class TransitionFunction():
	def __init__(self, width, height, obs_func, w, prob=0.1):
		# height - number (integer), width - number (integer), list_of_obstacles - list of tuples
		#assert(height >= 16)
		#assert(width >= 16)
		self.height = height
		self.width = width
		self.obs_func = obs_func
		self.w = w # controls how often phase changes ... phase will change after every w time steps
		self.p = 0 # later select randomly between 0, pi/2, pi, 3pi/2, 2pi
		self.prob = prob # probability with which agent moves with the wind

	def __call__(self, state, action,t):
		delta = Action.oned_to_twod(action.delta)
		t = t + 1 # one more than reward because reward is called after transition and t is maintained by reward. t maintained by reward for easy reset.
		new_list_of_obstacles = []
		obs_delta = self.obs_func(t)
		for obs in state.list_of_obstacles:
			new_obs = (obs[0] + obs_delta[0], obs[1]+obs_delta[1])
			if new_obs[0] >= self.width or new_obs[0] < 0 or new_obs[1] >= self.height or new_obs[1] < 0:
				print 'Obstacle moved outside of the grid!!!'
				sys.exit()
			new_list_of_obstacles.append(new_obs)

		# internal phase
		phase = self.phase(t-1) # phase computed on current time t not t+1
		#change delta based on internal phase
		if phase == 0: # up
			if np.random.uniform() < self.prob:
				delta = (delta[0]+0,delta[1]+1)
		elif phase == math.pi/4: # up and right
			if np.random.uniform() < self.prob:
				delta = (delta[0]+1,delta[1]+1)
		elif phase == math.pi/2: # right
			if np.random.uniform() < self.prob:
				delta = (delta[0]+1,delta[1]+0)
		elif phase == 3*math.pi/4: # down and right
			if np.random.uniform() < self.prob:
				delta = (delta[0]+1,delta[1]-1)
		elif phase == math.pi: # down
			if np.random.uniform() < self.prob:
				delta = (delta[0]+0,delta[1]-1)
		elif phase == 5*math.pi/4: # down and left
			if np.random.uniform() < self.prob:
				delta = (delta[0]-1,delta[1]-1)
		elif phase == 3*math.pi/2: # left
			if np.random.uniform() < self.prob:
				delta = (delta[0]-1,delta[1]+0)
		elif phase == 7*math.pi/4: # up and left
			if np.random.uniform() < self.prob:
				delta = (delta[0]-1,delta[1]+1)
		else:
			print 'Unknown phase'
			sys.exit()
		# compute new coordinates here. Stay within boundary and don't move over obstacles (new).
		new_coordinates = (max(min(state.coordinates[0] + delta[0],self.width-1),0), max(min(state.coordinates[1] + delta[1],self.height-1),0))
		if new_coordinates in new_list_of_obstacles:
			# do stuff here - option 1. Remain where you are. This should be sufficient. If not, then try moving right, left down or up
			if state.coordinates not in new_list_of_obstacles:
				new_coordinates = state.coordinates # best case scenario ... stay where you are
			else:
				if (max(min(state.coordinates[0]+1,self.width-1),0), state.coordinates[1]) not in new_list_of_obstacles: # right
					new_coordinates = (max(min(state.coordinates[0]+1,self.width-1),0), state.coordinates[1])
					#print 'Warning at transition 1'
				elif (max(min(state.coordinates[0]-1,self.width-1),0), state.coordinates[1]) not in new_list_of_obstacles: # left
					new_coordinates = (max(min(state.coordinates[0]-1,self.width-1),0), state.coordinates[1])
					#print 'Warning at transition 2'
				elif (state.coordinates[0], max(min(state.coordinates[1]-1,self.height-1),0)) not in new_list_of_obstacles: # down
					new_coordinates = (state.coordinates[0], max(min(state.coordinates[1]-1,self.height-1),0))
					#print 'Warning at transition 3'
				elif (state.coordinates[0], max(min(state.coordinates[1]+1,self.height-1),0)) not in new_list_of_obstacles: # up
					#print 'Warning at transition 4'
					new_coordinates = (state.coordinates[0], max(min(state.coordinates[1]+1,self.height-1),0))
				else:
					print 'There is an obstacle for every transition!!!'
					sys.exit()

		new_state = State(new_coordinates, new_list_of_obstacles)
		return new_state

	def phase(self,t):
		#return ((math.floor(t/self.w)/2 + (self.p/math.pi)) % 2)*math.pi # t1 and t2
		#return ((math.floor(t/self.w)/4 + (self.p/math.pi)) % 2)*math.pi # t3
		if t == 0:
			self.old_t = t
			self.curr_phase =  np.random.randint(0,high=4)*math.pi/2
		else:
			if math.floor(self.old_t/self.w) == math.floor(t/self.w):
				return self.curr_phase
			else:
				self.old_t = t
				self.curr_phase =  np.random.randint(0,high=4)*math.pi/2

		return self.curr_phase


class ExperienceReplay():
	def __init__(self, max_memory_size = 100):
		self.memory = []
		self.oldest = -1
		self.max_memory_size = max_memory_size
	
	def add(self, experience):
		if len(self.memory) < self.max_memory_size: 
			self.memory.append(experience)
			self.oldest = 0
		else:
			self.memory.insert(self.oldest, experience)
			self.oldest = (self.oldest + 1) % self.max_memory_size

	def sample(self):
		idx = np.random.randint(0, high=len(self.memory))
		return self.memory[idx]
			


def epsilon_greedy_linear_decay(action_vector, n_episodes, n, low=0.1, high=0.9):
	if n <= n_episodes:
		eps = ((low-high)/n_episodes)*n + high
	else:
		eps = low

	if np.random.uniform() > eps:
		return np.argmax(action_vector)
	else:
		return np.random.randint(low=0, high=5)

def epsilon_greedy(action_vector, eps):
	if np.random.uniform() > eps:
		return np.argmax(action_vector)
	else:
		return np.random.randint(low=0, high=5)

def sample_start(set_diff):
	return random.choice(set_diff)

def main():
	height = 12
	width = 12
	max_episode_length = 600
	n_episodes = 50000
	n_copy_after = 1000
	burn_in = 100
	policy_type = int(sys.argv[1])
	probab = float(sys.argv[-1])
	if policy_type != 3:
		policy_checkpoint = sys.argv[2]
	visualize_flag = False

	obstacles = create_obstacles(width,height)

	T = TransitionFunction(width,height,obstacle_movement,4, prob=probab)
	R = RewardFunction(penalty=-1,goal_1_coordinates=(11,0),goal_1_func=goal_1_reward_func,goal_2_coordinates=(11,11),goal_2_func=goal_2_reward_func, w1=math.pi/8, w2=math.pi/8)
	M = ExperienceReplay(max_memory_size=1000)

	if policy_type != 3:
		policy = torch.load(policy_checkpoint)
	
	if visualize_flag:
		app = QtGui.QApplication(sys.argv)
		visualizer = QTVisualizer('Varying transition dynamics')

	# testing with greedy policy
	print 'Using greedy policy ...'
	start_loc = (0,5)
	average_total_reward = 0
	average_step_count = 0
	for _ in range(1000):
		total_reward = 0
		step_count = 0
		s = State(start_loc, obstacles)
		R.reset()
		if policy_type != 3:
			policy.reset()
		while R.terminal == False:
			if visualize_flag:
				visualizer.draw_world(agent=s.coordinates, obstacles=s.list_of_obstacles, goals=[(11,0),(11,11)])
				q_refresh()

			phase = T.phase(R.t)
			if policy_type == 0:
				x = Variable(torch.from_numpy(s.state).type(dtype), requires_grad=False).unsqueeze(0)
				q = policy.forward(x)
				a = Action(np.argmax(q.data.cpu().numpy()))
			elif policy_type == 1:
				inp = np.concatenate((s.state,np.asarray([phase])))
				x = Variable(torch.from_numpy(inp).type(dtype), requires_grad=False).unsqueeze(0)
				q = policy.forward(x)
				a = Action(np.argmax(q.data.cpu().numpy()))
			elif policy_type == 2:
				x = Variable(torch.from_numpy(s.state).type(dtype), requires_grad=False).unsqueeze(0)
				q = policy.forward(x, phase)
				a = Action(np.argmax(q.data.cpu().numpy()))
			elif policy_type == 3:
				a = Action(np.random.randint(0,high=5))
			t = R.t
			s_prime = T(s,a,t)
			reward = R(s,a,s_prime)
			total_reward += reward
			step_count += 1
			s = s_prime



		average_total_reward += total_reward
		average_step_count += step_count

	print 'Average total reward', average_total_reward/1000.0
	print 'Average step count', average_step_count/1000.0

if __name__ == '__main__':
	main()
