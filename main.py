import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

import torch_geometric.nn as pyg_nn
import torch_geometric.utils as pyg_utils

import time
from datetime import datetime

import networkx as nx
import numpy as np

from torch_geometric.datasets import TUDataset
from torch_geometric.datasets import Planetoid
from torch_geometric.data import DataLoader

import torch_geometric.transforms as T

from tensorboardX import SummaryWriter
from sklearn.manifold import TSNE
import matplotlib.pyplot as plt

class GNNStack(nn.Module):

    def __init__(self, input_dim, hidden_dim, output_dim, lp_path_num, lp_length, lp_dim, ns_length, ns_dim, task='node'):
        """
        :param input_dim: max(dataset.num_node_features, 1)
        :param hidden_dim: 32
        :param output_dim: dataset.num_classes
        :param task: 'node' or 'graph'
        """
        super(GNNStack, self).__init__()
        self.task = task


        # LP: (lp_path_num, lp_length, lp_dim)
        self.lp_conv1 = nn.Conv2d(lp_path_num, 6, kernel_size=5, padding=2) # (6, lp_length, lp_dim)
        self.lp_pool1 = nn.MaxPool2d(2, 2)
        self.lp_conv2 = nn.Conv2d(6, 16, kernel_size=5, padding=2) # (16, lp_length / 2, lp_dim / 2)
        self.lp_pool2 = nn.MaxPool2d(2, 2)
        self.lp_fc = nn.Linear(16 * (lp_length/4) * (lp_dim/4), 128)

        # NS: (1, ns_length, ns_dim)
        self.ns_conv1 = nn.Conv2d(1, 6, kernel_size=5, padding=2)  # (6, ns_length, ns_dim)
        self.ns_pool1 = nn.MaxPool2d(2, 2)
        self.ns_conv2 = nn.Conv2d(6, 16, kernel_size=5, padding=2)  # (16, ns_length / 2, ns_dim / 2)
        self.ns_pool2 = nn.MaxPool2d(2, 2)
        self.ns_fc = nn.Linear(16 * (ns_length / 4) * (ns_dim / 4), 128)

        self.convs = nn.ModuleList()
        self.convs.append(self.build_conv_model(input_dim, hidden_dim))
        self.lns = nn.ModuleList()
        self.lns.append(nn.LayerNorm(hidden_dim))
        self.lns.append(nn.LayerNorm(hidden_dim))
        for l in range(2):
            self.convs.append(self.build_conv_model(hidden_dim, hidden_dim))

        # post-message-passing
        self.post_mp = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim), nn.Dropout(0.25),
            nn.Linear(hidden_dim, output_dim))
        if not (self.task == 'node' or self.task == 'graph'):
            raise RuntimeError('Unknown task.')

        self.dropout = 0.25
        self.num_layers = 3 # len of self.convs

    def build_conv_model(self, input_dim, hidden_dim):
        # refer to pytorch geometric nn module for different implementation of GNNs.
        if self.task == 'node':
            return pyg_nn.GCNConv(input_dim, hidden_dim)
        else:
            return pyg_nn.GINConv(nn.Sequential(nn.Linear(input_dim, hidden_dim),
                                  nn.ReLU(), nn.Linear(hidden_dim, hidden_dim)))

    def forward(self, data):
        # x: [2265, 3]
        # edge_index: [2, 8468]
        # batch: 2265
        xs, edge_index, batch = data.xs, data.edge_index, data.batch
        # if data.num_node_features == 0:
        #   x = torch.ones(data.num_nodes, 1)

        # TODO: combine DEF, REF, PDT, LP, NS --> x
        x_pdt = xs['pdt']
        x_ref = xs['ref']
        x_def = xs['def']
        x_lp = xs['lp']
        x_ns = xs['ns']




        for i in range(self.num_layers):
            x = self.convs[i](x, edge_index)
            emb = x
            # emb: [2265, 32]
            x = F.relu(x)
            x = F.dropout(x, p=self.dropout, training=self.training)
            if not i == self.num_layers - 1:
                x = self.lns[i](x)

        if self.task == 'graph':
            x = pyg_nn.global_mean_pool(x, batch)

        x = self.post_mp(x)

        return emb, F.log_softmax(x, dim=1) # dim (int): A dimension along which log_softmax will be computed.

    def loss(self, pred, label):
        return F.nll_loss(pred, label)

class CustomConv(pyg_nn.MessagePassing):
    def __init__(self, in_channels, out_channels):
        super(CustomConv, self).__init__(aggr='add')  # "Add" aggregation.
        self.lin = nn.Linear(in_channels, out_channels)
        self.lin_self = nn.Linear(in_channels, out_channels)

    def forward(self, x, edge_index):
        # x has shape [N, in_channels]
        # edge_index has shape [2, E]

        # Add self-loops to the adjacency matrix.
        edge_index, _ = pyg_utils.remove_self_loops(edge_index)

        # Transform node feature matrix.
        self_x = self.lin_self(x)
        #x = self.lin(x)

        return self_x + self.propagate(edge_index, size=(x.size(0), x.size(0)), x=self.lin(x))

    def message(self, x_i, x_j, edge_index, size):
        # Compute messages
        # x_j has shape [E, out_channels]

        row, col = edge_index
        deg = pyg_utils.degree(row, size[0], dtype=x_j.dtype)
        deg_inv_sqrt = deg.pow(-0.5)
        norm = deg_inv_sqrt[row] * deg_inv_sqrt[col]

        return x_j

    def update(self, aggr_out):
        # aggr_out has shape [N, out_channels]
        return aggr_out


def test(loader, model, is_validation=False):
    model.eval()

    correct = 0
    for data in loader:
        with torch.no_grad():
            emb, pred = model(data)
            pred = pred.argmax(dim=1)
            label = data.y

        if model.task == 'node':
            mask = data.val_mask if is_validation else data.test_mask
            # node classification: only evaluate on nodes in test set
            pred = pred[mask]
            label = data.y[mask]

        correct += pred.eq(label).sum().item()

    if model.task == 'graph':
        total = len(loader.dataset)
    else:
        total = 0
        for data in loader.dataset:
            total += torch.sum(data.test_mask).item()
    return correct / total


def train(dataset, task, writer):
    if task == 'graph':
        data_size = len(dataset)
        print("data_size:", data_size)
        loader = DataLoader(dataset[:int(data_size * 0.8)], batch_size=64, shuffle=True)
        test_loader = DataLoader(dataset[int(data_size * 0.8):], batch_size=64, shuffle=True)
    else:
        test_loader = loader = DataLoader(dataset, batch_size=64, shuffle=True)

    # build model
    model = GNNStack(input_dim=max(dataset.num_node_features, 1),
                     hidden_dim=32,
                     output_dim=dataset.num_classes,
                     task=task)
    print("len(model.convs):", len(model.convs))

    opt = optim.Adam(model.parameters(), lr=0.01)

    # train
    for epoch in range(800):
        total_loss = 0
        model.train()
        for batch in loader:
            # print(batch.train_mask, '----')
            opt.zero_grad()
            embedding, pred = model(batch)
            label = batch.y
            if task == 'node':
                pred = pred[batch.train_mask]
                label = label[batch.train_mask]
            loss = model.loss(pred, label)
            loss.backward()
            opt.step()
            total_loss += loss.item() * batch.num_graphs
        total_loss /= len(loader.dataset)
        writer.add_scalar("loss", total_loss, epoch)

        if epoch % 10 == 0:
            test_acc = test(test_loader, model)
            print("Epoch {}. Loss: {:.4f}. Test accuracy: {:.4f}".format(
                epoch, total_loss, test_acc))
            writer.add_scalar("test accuracy", test_acc, epoch)

    return model


if __name__ == '__main__':
    writer = SummaryWriter("./log/" + datetime.now().strftime("%Y%m%d-%H%M%S"))

    dataset = TUDataset(root='/tmp/PROTEINS', name='PROTEINS')
    dataset = dataset.shuffle()
    task = 'graph'

    model = train(dataset, task, writer)