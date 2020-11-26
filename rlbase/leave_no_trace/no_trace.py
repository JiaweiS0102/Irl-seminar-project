from ddpg import ReplayBuffer

import copy
import time
import os

import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter
from torch import optim
from torch import nn

import rlbase.algorithms.ddpg.core as core

from typing import Optional, Any, Mapping, List
from dataclasses import dataclass
import click

import gym


@dataclass
class DDPG:
    env: object
    ac_kwargs: dict = {}
    actor_criticClass: object = core.MLPActorCritic
    memory_size: int = int(1e6)
    steps_per_epoch: int = 5000
    epochs: int = 100
    max_eps_len: int = 1000
    pi_lr: float = 1e-3
    q_lr: float = 1e-3
    seed: int = 0
    act_noise_std: float = .1
    exploration_steps: int = 10000
    update_frequency: int = 50
    start_update: int = 1000
    batch_size: int = 128
    gamma: float = .99
    eval_episodes: int = 5
    polyyak: float = .995

    args: Mapping[Any, Any]
    """
        Deep Deterministic Policy Gradients (DDPG)

        Args
        ---
        ac_kwargs (dict): Actor Critic module parameters

        memory_size (int): Replay buffer limit for transition
            storage

        steps_per_epoch (int): Number of steps interact with the
            environment per episode

        epochs (int): Number of updates to perform on the agent

        max_eps_len (int): Maximum length to have for each episode
            before it is terminated

        pi_lr(float): Policy learning rate

        q_lr(float): Q-function learning rate

        act_noise_std (float): Stddev for noise added to actions at
            training time

        exploration_steps (int): Number of steps to select actions
            at random uniform before turning to the policy.
            Policy is deterministic

        update_frequency (int): Interval of steps at which to update
            the actor

        start_update (int): The number of steps to interact with
            the environment before starting updates.
            This is meant to collect enough transitions in the
            replay buffer.

        batch_size (int): SGD mini batch size

        gamma (float): Rewards decay factor

        polyyak (float): Factor for interpolation during updating
            of target network

        eval_episodes (int): Number of episodes to evaluate the agent
    """

    def __init__(self, **args):

        np.random.seed(self.seed)
        torch.manual_seed(self.seed)

        device = torch.device(args.get('device')) if args.get('device') else \
            torch.device('cpu' if not torch.cuda.is_available else 'cuda')

        env = self.env
        act_dim = env.action_space.shape[0]
        obs_dim = env.observation_space.shape[0]
        act_limit = env.action_space.high[0]

        self.act_dim = act_dim
        self.obs_dim = obs_dim
        self.act_limit = act_limit

        self.rep_buffer = ReplayBuffer(size=self.memory_size,
                                       act_dim=act_dim,
                                       obs_dim=obs_dim, device=device)

        self.actor_critic = self.actor_criticClass(obs_dim,
                                                   act_dim, act_limit,
                                                   **ac_kwargs).to(device)
        self.actor_critic_reset = self.actor_criticClass(obs_dim,
                                                         act_dim, act_limit,
                                                         **ac_kwargs).to(device)
        self.ac_target = copy.deepcopy(self.actor_critic).to(device)
        self.ac_target_reset = copy.deepcopy(
            self.actor_critic_reset).to(device)

        pi_lr, q_lr = self.pi_lr, self.q_lr

        q_optim = optim.Adam(self.actor_critic.q.parameters(), lr=q_lr)
        pi_optim = optim.Adam(self.actor_critic.pi.parameters(), lr=pi_lr)

        q_reset_optim = optim.Adam(
            self.actor_critic_reset.q.parameters(), lr=q_lr)
        pi_reset_optim = optim.Adam(
            self.actor_critic_reset.pi.parameters(), lr=pi_lr)

        self.optimizers = {'q_optim': q_optim, 'pi_optim': pi_optim,
                           'pi_reset_optim': pi_reset_optim, 'q_reset_optim': q_reset_optim}

        self.q_loss_f = torch.nn.MSELoss()

        print(f'param counts: {core.count(self.actor_critic)}, ' +
              f'reset:{core.count(self.actor_critic_reset)}\n')

        for param in self.ac_target.parameters():
            param.requires_grad = False
        for param in self.ac_target_reset.parameters():
            param.requires_grad = False

        run_t = time.strftime('%Y-%m-%d-%H-%M-%S')
        path = os.path.join(
            'data', env.unwrapped.spec.id + args.get('env_name', '') + '_' + run_t)

        self.logger = SummaryWriter(log_dir=path)

        q_losses, pi_losses = [], []

    def encode_action(self, action):
        """
            Add Gaussian noise to action
        """

        epsilon = np.random.rand(self.act_dim) * self.act_noise_std
        act_limit = self.act_limit

        return np.clip(action + epsilon, -act_limit, act_limit)

    def eval_agent(self, epoch, kwargs=args):
        """
            Evaluate agent
        """
        # env = kwargs['test_env']
        print(f'\n\nEvaluating agent\nEpisodes [{self.eval_episodes}]')
        all_eps_len, all_eps_ret = [], []
        for _ in range(self.eval_episodes):

            eps_len, eps_ret, obs = 0, 0, self.env.reset()
            for _ in range(self.steps_per_epoch):
                act = self.actor_critic.act(
                    torch.from_numpy(obs).float().to(device))

                obs_n, rew, done, _ = self.env.step(act)

                obs = obs_n
                eps_len += 1
                eps_ret += rew

                if done or eps_len == self.max_eps_len:
                    all_eps_len.append(eps_len)
                    all_eps_ret.append(eps_ret)

                    break

            self.logger.add_scalar('Evaluation/Return', eps_ret, epoch)
            self.logger.add_scalar('Evaluation/EpsLen', eps_len, epoch)

        return np.mean(all_eps_len), np.mean(all_eps_ret)

    def compute_pi_loss(self, data, ac: object):
        """
            Policy Loss function

            ac: Actor critic to compute loss for
        """
        states = data['obs']
        q = ac.q
        pi = ac.pi

        return -(q(states, pi(states))).mean()

    def compute_q_loss(self, data, gamma: float, ac: List[object]):
        """
            Q function loss

            ac: [Ac, Ac_target] Actor critic to compute loss for

        """
        rew = data['rew']
        dones = data['dones']
        n_states = data['obs_n']
        states = data['obs']
        actions = data['act']

        q = ac[0].q
        pi_target = ac[1].pi
        q_target = ac[1].q

        q_pred = q(states, actions)
        with torch.no_grad():
            target = rew + gamma * (1 - dones) * \
                q_target(n_states, pi_target(n_states))
        loss = self.q_loss_f(q_pred, target)

        return loss

    def zero_optim(self, optimizer, set_none: Optional[bool] = False):
        """
            Set Grads to None
        """
        if not set_none:
            optimizer.zero_grad()
            return
        for group in optimizer.param_groups:
            for param in group['params']:
                param.grad = None

    def update(self, n_epoch, data, ac_items: List[object], pi_optim: object, q_optim: object):
        """
            Policy and Q function update

            ac: [ac, ac_target] Current actor critic
        """

        ac, ac_target = ac_items
        # update Q
        zero_optim(q_optim)
        q_loss = self.compute_q_loss(data, self.gamma, ac_items)
        q_loss.backward()
        q_optim.step()

        # update pi
        for p in ac.q.parameters():
            p.requires_grad = False

        zero_optim(pi_optim)
        pi_loss = self.compute_pi_loss(data, ac)
        pi_loss.backward()
        pi_optim.step()

        nonlocal q_losses, pi_losses

        q_losses += [q_loss.item()]
        pi_losses += [pi_loss.item()]

        self.logger.add_scalar('Loss/q', q_loss.item(), n_epoch)
        self.logger.add_scalar('Loss/pi', pi_loss.item(), n_epoch)

        for p in ac.q.parameters():
            p.requires_grad = True
        # update target
        with torch.no_grad():
            phi_params = ac.parameters()
            phi_target_params = ac_target.parameters()
            for param, target_param in zip(
                phi_params, phi_target_params
            ):
                # p(target) + (1 - p)param
                target_param.data.mul_(self.polyyak)
                target_param.data.add_(param.data * (1-self.polyyak))

    def run_training_loop(self):
        """
            Trains the agent by running the specified
            number of steps and agent udpates
        """
        start_time = time.time()
        obs = env.reset()
        eps_len, eps_ret = 0, 0

        for epoch in range(epochs):
            for t in range(steps_per_epoch):
                ...
