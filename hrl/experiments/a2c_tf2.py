import numpy as np
import tensorflow as tf
import tensorflow.keras.layers as kl
import tensorflow.keras.losses as kls
import tensorflow.keras.optimizers as ko
from gym_minigrid.wrappers import FullyObsWrapper
from tqdm import tqdm

from hrl.envs.four_rooms import FourRooms
from hrl.envs.wrappers import SimplifyObsSpace, SimplifyActionSpace


class ProbabilityDistribution(tf.keras.Model):
    def call(self, logits):
        # sample a random categorical action from given logits
        return tf.squeeze(tf.random.categorical(logits, 1), axis=-1)


class Model(tf.keras.Model):
    def __init__(self, num_actions):
        super().__init__('mlp_policy')
        # no tf.get_variable(), just simple Keras API
        self.actor = kl.Dense(128, activation='relu')
        self.critic = kl.Dense(128, activation='relu')
        self.value = kl.Dense(1, name='value')
        # logits are unnormalized log probabilities
        self.logits = kl.Dense(num_actions, name='policy_logits')
        self.dist = ProbabilityDistribution()
    
    def call(self, inputs):
        # inputs is a numpy array, convert to Tensor
        x = tf.convert_to_tensor(inputs, dtype=tf.float32)
        # separate hidden layers from the same input tensor
        hidden_logs = self.actor(x)
        hidden_vals = self.critic(x)
        return self.logits(hidden_logs), self.value(hidden_vals)
    
    def action_value(self, obs):
        # executes call() under the hood
        logits, value = self.predict(obs)
        action = self.dist.predict(logits)
        # a simpler option, will become clear later why we don't use it
        # action = tf.random.categorical(logits, 1)
        return np.squeeze(action, axis=-1), np.squeeze(value, axis=-1)


class A2CAgent:
    def __init__(self, model):
        # hyperparameters for loss terms
        self.params = {'value': 0.5, 'entropy': 0.0001, 'gamma': 0.99}
        self.model = model
        self.model.compile(
            optimizer=ko.RMSprop(lr=0.0007),
            # define separate losses for policy logits and value estimate
            loss=[self._logits_loss, self._value_loss]
        )
    
    def test(self, env, render=False):
        obs, done, ep_reward = env.reset(), False, 0
        obs = np.float32(obs.flatten())
        step = 0
        while not done:
            step += 1
            if step % 1000 == 0:
                print(step)
            action, _ = self.model.action_value(obs[None, :])
            obs, reward, done, _ = env.step(action)
            obs = np.float32(obs.flatten())
            ep_reward += reward
            if render:
                env.render()
        return step
    
    def train(self, env, batch_sz=32, updates=1000):
        # storage helpers for a single batch of data
        actions = np.empty((batch_sz,), dtype=np.int32)
        rewards, dones, values = np.empty((3, batch_sz))
        observations = np.empty(
            (batch_sz,) + (np.product(env.observation_space.shape),))
        
        # training loop: collect samples, send to optimizer, repeat updates times
        ep_rews = [0.0]
        next_obs = env.reset()
        next_obs = np.float32(next_obs.flatten())
        
        for update in tqdm(range(updates)):
            for step in range(batch_sz):
                observations[step] = next_obs.copy()
                actions[step], values[step] = self.model.action_value(
                    next_obs[None, :])
                next_obs, rewards[step], dones[step], _ = env.step(
                    actions[step])
                next_obs = np.float32(next_obs.flatten())
                
                ep_rews[-1] += rewards[step]
                if dones[step]:
                    ep_rews.append(0.0)
                    next_obs = env.reset()
                    next_obs = np.float32(next_obs.flatten())
            
            _, next_value = self.model.action_value(next_obs[None, :])
            returns, advs = self._returns_advantages(rewards, dones, values,
                                                     next_value)
            # A trick to input actions and advantages through same API
            acts_and_advs = np.stack((actions, advs)).T
            # Performs a full training step on the collected batch
            losses = self.model.train_on_batch(
                x=observations,
                y=[acts_and_advs, returns]
            )
            print(losses)
        
        return ep_rews
    
    def _returns_advantages(self, rewards, dones, values, next_value):
        # Last value is the bootstrap value estimate of future states
        returns = np.append(np.zeros_like(rewards), next_value, axis=-1)
        # Returns are calculated as discounted sum of future rewards
        for t in reversed(range(rewards.shape[0])):
            returns[t] = rewards[t] + self.params['gamma'] * returns[t + 1] *\
                         (1 - dones[t])
        returns = returns[:-1]
        # advantages are returns - baseline, value estimates in our case
        advantages = returns - values
        return returns, advantages
    
    def _value_loss(self, returns, value):
        # value loss is typically MSE between value estimates and returns
        return self.params['value'] * kls.mean_squared_error(returns, value)
    
    def _logits_loss(self, acts_and_advs, logits):
        # a trick to input actions and advantages through same API
        actions, advantages = tf.split(acts_and_advs, 2, axis=-1)
        # sparse categorical CE loss obj that supports sample_weight arg on call()
        # from_logits argument ensures transformation into normalized probabilities
        weighted_sparse_ce = kls.SparseCategoricalCrossentropy(from_logits=True)
        # policy loss is defined by policy gradients, weighted by advantages
        # note: we only calculate the loss on the actions we've actually taken
        actions = tf.cast(actions, tf.int32)
        policy_loss = weighted_sparse_ce(actions, logits,
                                         sample_weight=advantages)
        # entropy loss can be calculated via CE over itself
        entropy_loss = kls.categorical_crossentropy(logits, logits,
                                                    from_logits=True)
        # here signs are flipped because optimizer minimizes
        return policy_loss - self.params['entropy'] * entropy_loss


if __name__ == '__main__':
    # Create environment
    env = SimplifyActionSpace(SimplifyObsSpace(FullyObsWrapper(FourRooms())))
    env.max_steps = 1000000
    
    # Create model
    model = Model(num_actions=env.action_space.n)
    
    # Create agent
    agent = A2CAgent(model)
    
    rewards_history = agent.train(env, updates=100000)
    print("Finished training, testing...")
    print(f"Took {agent.test(env)} steps")
