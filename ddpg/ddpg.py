import torch
import torch.nn as nn
import numpy as np
import gym
import quanser_robots

from buffer import ReplayBuffer
from noise import OrnsteinUhlenbeck
from critic_torch import Critic
from actor_torch import Actor

from tensorboardX import SummaryWriter


class DDPG(object):
    """
    Deep Deterministic Policy Gradient (DDPG) model

    :param env: (Gym Environment) gym environment to learn from
    :param noise: (Noise) the noise to learn with
    :param buffer_capacity: (int) capacity of the replay buffer
    :param batch_size: (int) size of the sample batches
    :param gamma: (float) discount factor
    :param tau: (float) soft update coefficient
    :param episodes: (int) number of episodes to make
    :param learning_rate: (float) learning rate of the optimization step
    :param episode_length: (int) length of an episode (= training steps per episode)
    :param actor_layers: (int, int) size of the layers of the policy network
    :param critic_layers: (int, int) size of the layers of the critic network
    :param log: (bool) flag for logging
    :param render: (bool) flag if to render while training or not
    :param save: (bool) flag if to save the model if finished
    :param save_path: (str) path for saving and loading a model

    """
    def __init__(self, env, noise=None, buffer_capacity=1e6, batch_size=64,
                 gamma=0.99, tau=0.001, episodes=int(1e4), learning_rate=1e-3,
                 episode_length=3000, actor_layers=None, critic_layers=None,
                 log=True, render=True, save=True, save_path="ddpg_model.pt"):
        # initialize env and read out shapes
        self.env = env
        self.state_shape = self.env.observation_space.shape[0]
        self.action_shape = self.env.action_space.shape[0]
        self.action_range = env.action_space.high[0]
        # initialize noise/buffer/loss
        self.noise = noise if noise is not None else OrnsteinUhlenbeck(self.action_shape)
        self.buffer = ReplayBuffer(buffer_capacity)
        self.loss = nn.MSELoss()
        # initialize hyperparameters
        self.batch_size = batch_size
        self.gamma = gamma
        self.tau = tau
        self.episodes = episodes
        self.episode_length = episode_length
        # initialize networks and optimizer
        self.actor = Actor(self.state_shape, self.action_shape, layer1=actor_layers[0], layer2=actor_layers[1]) \
            if actor_layers is not None else Actor(self.state_shape, self.action_shape)
        self.target_actor = Actor(self.state_shape, self.action_shape, layer1=actor_layers[0], layer2=actor_layers[1]) \
            if actor_layers is not None else Actor(self.state_shape, self.action_shape)
        self.critic = Critic(self.state_shape, self.action_shape, layer1=critic_layers[0], layer2=critic_layers[1]) \
            if actor_layers is not None else Critic(self.state_shape, self.action_shape)
        self.target_critic = Critic(self.state_shape, self.action_shape, layer1=critic_layers[0], layer2=critic_layers[1]) \
            if actor_layers is not None else Critic(self.state_shape, self.action_shape)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=learning_rate)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=learning_rate)
        for target_param, param in zip(self.target_actor.parameters(),
                                       self.actor.parameters()):
            target_param.data.copy_(param.data)
            target_param.requires_grad = False

        for target_param, param in zip(self.target_critic.parameters(),
                                       self.critic.parameters()):
            target_param.data.copy_(param.data)
            target_param.requires_grad = False
        # fill buffer with random transitions
        self._random_trajectory(self.batch_size)
        # control/log variables or flags
        self.episode = 0
        self.log = log
        self.render = render
        self.save = save
        self.save_path = save_path

    def _random_trajectory(self, length):
        """
        pushes a given number of random transitions on the buffer
        :param length: (int) number of random actions to take
        """
        observation = self.env.reset()
        for i in range(0, length):
            action = self.env.action_space.sample()
            new_observation, reward, done, _ = self.env.step(action)
            self.buffer.push(observation, action, reward, new_observation)
            observation = new_observation
            if done:
                observation = self.env.reset()

    def _select_action(self, observation, train=True):
        """
        selects a action based on the policy(/target policy) and a given state
        :param observation: (State) the state the decision is based on
        :param noise: (bool) a flag determining wether to add noise to the action or not
        :return: (Action) the action taken following the policy plus noise
                 (target policy without noise if not training)
        """
        obs = torch.tensor(observation).float()
        a = self.actor(obs).detach().numpy()
        a = a + self.noise.iteration() if train else self.target_actor(obs).detach().numpy()
        a = a * self.action_range
        a = np.clip(a, a_min=-self.action_range,
                    a_max=self.action_range)
        return a

    def _sample_batches(self, size):
        """
        samples corresponding batches of a given size for all transition elements
        :param size: (int) size of batches
        :return: ([State],[Action],[Reward],[State]) tuple of all batches
        """
        sample = self.buffer.sample(size)
        state_batch, action_batch, reward_batch, next_state_batch = \
            self.buffer.batches_from_sample(sample, self.batch_size)
        state_batch, action_batch, reward_batch, next_state_batch = \
            torch.tensor(state_batch).float(), torch.tensor(action_batch).float(),\
            torch.tensor(reward_batch).float(), torch.tensor(next_state_batch).float()
        return state_batch, action_batch, reward_batch, next_state_batch

    def _soft_update(self):
        """
        soft-updates the target network with respect to the soft-update coefficent
        """
        for target_param, param in zip(self.target_critic.parameters(),
                                       self.critic.parameters()):
            target_param.data.copy_(target_param.data * (1.0 - self.tau) + param.data * self.tau)

        for target_param, param in zip(self.target_actor.parameters(),
                                       self.actor.parameters()):
            target_param.data.copy_(target_param.data * (1.0 - self.tau) + param.data * self.tau)

    def train(self, episodes=None, episode_length=None, render=None, save=None, save_path=None, log=None):
        """
        trains the model
        :param episodes: (int) number of episodes to make
        :param episode_length: (int) length of an episode (= training steps per episode)
        :param render: (bool) flag if to render while training
        :param save: (bool) flag if to save the model after training
        :param save_path: (str) path where to save the model
        :param log: (bool) flag for logging messages and recording
        """
        rend = render if render is not None else self.render
        sf = save if save is not None else self.save
        sf_path = save_path if save_path is not None else self.save_path
        ep = episodes if episodes is not None else self.episodes
        it = episode_length if episode_length is not None else self.episode_length

        # initialize logging
        log_f = log if log is not None else self.log
        if log_f:
            writer = SummaryWriter()
            iteration = 0
            summed_rew = 0
            summed_q = 0
            summed_qloss = 0


        for episode in range(0, ep):
            self.noise.reset()
            observation = self.env.reset()

            for t in range(1, it + 1):
                # logging
                if iteration % 3000 == 0:
                    summed_rew = 0
                    summed_q = 0
                    summed_qloss = 0
                # choose action and execute it
                action = self._select_action(observation)
                new_observation, reward, done, _ = self.env.step(action)
                if rend is True:
                    self.env.render()
                # logging
                summed_rew += reward.item()
                summed_q += self.critic.log(torch.tensor(observation).float(),
                                            torch.tensor(action).float()).detach().item()
                iteration += 1

                # push transition onto the buffer
                self.buffer.push(observation, action, reward, new_observation)
                observation = new_observation

                # sample batches for training
                state_batch, action_batch, reward_batch, next_state_batch = \
                    self._sample_batches(self.batch_size)

                # update critic
                y = reward_batch + self.gamma * self.target_critic(next_state_batch, self.target_actor(next_state_batch))

                self.critic_optimizer.zero_grad()
                target = self.critic(state_batch, action_batch)
                loss_critic = self.loss(y, target)
                summed_qloss += loss_critic         # logging
                loss_critic.backward()
                self.critic_optimizer.step()

                # update actor
                self.actor_optimizer.zero_grad()
                loss_actor = self.critic(state_batch, self.actor(state_batch))
                loss_actor = -loss_actor.mean()
                loss_actor.backward()
                self.actor_optimizer.step()

                # update parameter
                self._soft_update()

            # logging
            if iteration % 3000 == 0:
                writer.add_scalar('data/mean_reward', summed_rew/3000, iteration)
                writer.add_scalar('data/mean_q', summed_q/3000, iteration)
                writer.add_scalar('data/mean_qloss', summed_qloss/3000, iteration)
            print("episode " + str(episode+1) + " of " + str(ep))

        # self._update_episode_log(ep, it)
        if sf is True:
            self.save_model(sf_path)


    # TODO eval
    def eval(self, episodes, episode_length, render=True):
        """
        method for evaluating current model
        :param episodes: (int) number of episodes for the evaluation
        :param episode_length: (int) length of a single episode (0 -> until done)
        :param render: (bool) flag if to render while evaluating
        :return: ([[float]],[float],[float]) tuple of arrays, size is number of episodes and one entry corresponds to one episode,
                 with Format (rewards, mean reward w.r.t. all previous rewards, mean q-value w.r.t. all previous episodes)
        """
        self.actor.eval()
        reward = []
        mean_reward = []
        mean_q = []
        for episode in range(episodes):
            observation = self.env.reset()
            reward_e = []
            mean_reward_e = []
            mean_q_e = []
            for step in range(episode_length):
                state = torch.tensor(observation).float()
                action = self._select_action(state, train=False)
                # obs = torch.tensor(observation).float()
                # q = self.critic.eval(state, action).item()
                # mean_q_e.append(q)
                new_observation, rew, done, _ = self.env.step(action)
                if render:
                    self.env.render()
                reward_e.append(rew.item())
                mean_reward_e.append(np.mean(reward_e).item())
                observation = new_observation
            reward.append(reward_e)
            mean_reward.append(mean_reward_e)
            mean_q.append(mean_q_e)

        self.actor.train()
        return reward, mean_reward, mean_q

    def save_model(self, path=None):
        """
        saves current model to a given path
        :param path: (str) saving path for the model
        """
        save_path = path if path is not None else self.save_path
        data = {
            'epoch': self.episode,
            'critic_state_dict': self.critic.state_dict(),
            'target_critic_state_dict': self.target_critic.state_dict(),
            'actor_state_dict': self.actor.state_dict(),
            'target_actor_state_dict': self.target_actor.state_dict(),
            'critic_optim_state_dict': self.critic_optimizer.state_dict(),
            'actor_optim_state_dict': self.actor_optimizer.state_dict()}
        torch.save(data, save_path)

    def load_model(self, path=None):
        """
        loads a model from a given path
        :param path: (str) loading path for the model
        """
        load_path = path if path is not None else self.save_path
        checkpoint = torch.load(load_path)
        self.episode = checkpoint['epoch']
        self.critic.load_state_dict(checkpoint['critic_state_dict'])
        self.target_critic.load_state_dict(checkpoint['target_critic_state_dict'])
        self.actor.load_state_dict(checkpoint['actor_state_dict'])
        self.target_actor.load_state_dict(checkpoint['target_actor_state_dict'])
        self.critic_optimizer.load_state_dict(checkpoint['critic_optim_state_dict'])
        self.actor_optimizer.load_state_dict(checkpoint['actor_optim_state_dict'])
