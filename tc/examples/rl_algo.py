import numpy as np
import ipdb
import torch
import torch.optim as optim
import torch.nn as nn
import torch.utils.data
import torch.nn.functional as F
import ipdb
from itertools import count
from collections import namedtuple
from torch.distributions import Categorical
import time
import tensor_comprehensions as tc
from visdom import Visdom
import my_utils

NB_HYPERPARAMS, INIT_INPUT_SZ = my_utils.NB_HYPERPARAMS, my_utils.INIT_INPUT_SZ
NB_EPOCHS = 10000
BATCH_SZ = 8
EPS_START = 0.9
EPS_END = 0.05
EPS_DECAY = 200
steps_done = 0

viz = Visdom(server="http://100.97.69.78")
win0 = viz.line(X=np.arange(NB_EPOCHS), Y=np.random.rand(NB_EPOCHS))
win1 = viz.line(X=np.arange(NB_EPOCHS), Y=np.random.rand(NB_EPOCHS))

code = """
def group_normalization(
    float(N, G, D, H, W) I, float(G, D) gamma, float(G, D) beta)
    -> (O, mean, var)
{
    mean(n, g) +=! I(n, g, r_d, r_h, r_w)
     var(n, g) +=! I(n, g, r_d, r_h, r_w) * I(n, g, r_d, r_h, r_w)

    O(n, g, d, h, w) = gamma(g, d)
      * ( I(n, g, d, h, w) - mean(n, g) * 4 )
      * rsqrt( var(n, g) * 4
            - mean(n, g) * mean(n, g) * 4 * 4
            + 1e-5)
      + beta(g, d)
}
"""

SavedAction = namedtuple('SavedAction', ['log_prob', 'value'])

class Predictor(nn.Module):
    def __init__(self, nb_inputs, nb_actions):
        super(Predictor, self).__init__()
        self.affine1 = nn.Linear(nb_inputs, 32)
        self.affine2 = nn.Linear(32, nb_actions)
        self.affine3 = nn.Linear(32, 1)

    def forward(self, x):
        tmp1 = F.relu(self.affine1(x))
        out_action = F.softmax(self.affine2(tmp1))
        out_value = self.affine3(tmp1)
        return out_action, out_value

class FullNetwork(nn.Module):
    def __init__(self, nb_hyperparams, init_input_sz, batch_size):
        super(FullNetwork, self).__init__()
        self.nb_hyperparams = nb_hyperparams
        self.init_input_sz = init_input_sz
        self.nets = [Predictor(init_input_sz + i, int(my_utils.cat_sz[i])) for i in range(nb_hyperparams)]
        self.nets = nn.ModuleList(self.nets)
        self.saved_actions = [[] for i in range(batch_size)]

    def select_action(self, x, i, batch_id, out_sz):
        geps = 0.1
        proba = np.random.rand()
        probs, state_value = self.nets[i](x)
        if(proba <= geps):
            probs = torch.FloatTensor([1./out_sz]*out_sz)
        m = Categorical(probs)
        action = m.sample()
        self.saved_actions[batch_id].append(SavedAction(m.log_prob(action), state_value))
        return action.item()

    def forward(self, x, batch_id):
        for i in range(self.nb_hyperparams):
            sym = self.select_action(x, i, batch_id, int(my_utils.cat_sz[i]))
            x = torch.cat([x, torch.FloatTensor([sym])])
        return x[INIT_INPUT_SZ:]

N, G, D, H, W = 5, 5, 5, 5, 5
I, gamma, beta = torch.randn(N, G, D, H, W).cuda(), torch.randn(G, D).cuda(), torch.randn(G, D).cuda()

init_input = (I, gamma, beta)
init_input_sz = np.array([N,G,D,H,W])
init_input_sz = torch.from_numpy(init_input_sz).float()

inp = init_input
my_utils.computeCat(inp)

net = FullNetwork(NB_HYPERPARAMS, INIT_INPUT_SZ, BATCH_SZ)
optimizer = optim.Adam(net.parameters())
eps = np.finfo(np.float32).eps.item()

tc_prog = tc.define(code, name="group_normalization")
my_utils.set_tcprog(tc_prog)

def finish_episode(final_rewards):
    saved_actions = net.saved_actions
    policy_losses = [[] for i in range(BATCH_SZ)]
    value_losses = [[] for i in range(BATCH_SZ)]
    final_rewards = torch.tensor(final_rewards)
    #final_rewards = (final_rewards - final_rewards.mean()) / (final_rewards.std() + eps)
    for batch_id in range(BATCH_SZ):
        for (log_prob, value) in saved_actions[batch_id]:
            reward = final_rewards[batch_id] - value.item()
            policy_losses[batch_id].append(-log_prob * reward)
            value_losses[batch_id].append(F.smooth_l1_loss(value, torch.tensor([final_rewards[batch_id]])))
    optimizer.zero_grad()
    vloss = torch.stack([torch.stack(value_losses[i]).sum() for i in range(BATCH_SZ)]).mean()
    ploss = torch.stack([torch.stack(policy_losses[i]).sum() for i in range(BATCH_SZ)]).mean()
    loss = ploss + vloss
    loss.backward()
    optimizer.step()
    del net.saved_actions[:]
    net.saved_actions = [[] for i in range(BATCH_SZ)]
    return vloss.item(), ploss.item()

INTER_DISP = 20

running_reward = -0.5
tab_rewards=[]
tab_best=[]
best=-0.5
v_losses=[]
p_losses=[]
best_options = np.zeros(13).astype(int)
for i in range(NB_EPOCHS):
    rewards = []
    for j in range(BATCH_SZ):
        out = net(init_input_sz,j)
        reward = -my_utils.evalTime(out.numpy().astype(int))
        reward=100*reward#+0.45
        rewards.append(reward)
    vloss, ploss = finish_episode(rewards)
    v_losses.append(vloss)
    p_losses.append(ploss)
    if(best < reward):
        best=reward
        best_options = out.numpy().astype(int)
        print(best_options)
    #best = max(best, np.max(rewards))
    running_reward = running_reward * 0.99 + np.mean(rewards) * 0.01
    tab_rewards.append(-running_reward)
    tab_best.append(-best)
    if i % INTER_DISP == 0:
        viz.line(X=np.column_stack((np.arange(i+1), np.arange(i+1))), Y=np.column_stack((np.array(tab_rewards), np.array(tab_best))), win=win0, opts=dict(legend=["Geometric run", "Best time"]))
        viz.line(X=np.column_stack((np.arange(i+1), np.arange(i+1))), Y=np.column_stack((np.array(v_losses), np.array(p_losses))), win=win1, opts=dict(legend=["Value loss", "Policy loss"]))
        print(-running_reward)
        print(-best)

print("Finally, best options are:")
print(best_options)