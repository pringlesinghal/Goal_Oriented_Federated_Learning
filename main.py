# imports
import wandb

wandb.login()

import torch
import torch.nn as nn
import torch.optim as optim

import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

from copy import deepcopy
from tqdm import tqdm
from itertools import chain, combinations
from math import comb
import pickle
import os

from data_preprocess import (
    load_mnist_flat,
    load_cifar10,
    NIIDClientSplit,
    synthetic_samples,
)
from model import NN, CNN
from dshap import convergenceTest

os.makedirs("./processed_data/mnist/", exist_ok=True)
os.makedirs("./processed_data/cifar10/", exist_ok=True)
os.makedirs("./processed_data/mnist_flat/", exist_ok=True)
os.makedirs("./processed_data/cifar10_flat/", exist_ok=True)

# global variables
wandb_config = {}
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
# device = "meta"
wandb_config["device"] = device


class Client:
    def __init__(self, data, targets, device):
        self.data = data.to(device)
        self.targets = targets.to(device)
        self.device = device
        self.length = len(self.data)

    def train(self, serverModel, criterion, E, B, learning_rate, momentum):
        """
        serverModel - server model
        criterion - loss function (model, data, targets)
        E - number of epochs
        B - number of batches

        returns clientModel.state_dict() after training
        """
        clientModel = deepcopy(serverModel)
        clientModel = clientModel.to(self.device)
        clientModel.load_state_dict(serverModel.state_dict())
        clientOptimiser = optim.SGD(
            clientModel.parameters(), lr=learning_rate, momentum=momentum
        )

        batch_indices = self.split_indices(B)

        for epoch in range(E):
            for batch in range(B):
                data_batch, targets_batch = self.get_subset(batch_indices[batch])
                clientOptimiser.zero_grad()
                loss = criterion(clientModel, data_batch, targets_batch)
                loss.backward()
                clientOptimiser.step()

        self.model = clientModel
        return clientModel.state_dict()

    def loss(self, model, criterion):
        """
        criterion - loss function (model, data, targets)
        """
        model.eval()
        with torch.no_grad():
            loss = criterion(model, self.data, self.targets)
        model.train()
        return float(loss.cpu())

    def accuracy(self):
        self.model.eval()
        with torch.no_grad():
            scores = self.model(self.data)
            _, predictions = scores.max(1)
            num_correct = torch.sum(predictions == self.targets)
            total = self.length
            accuracy = num_correct / total
        self.model.train()
        return float(accuracy.cpu())

    def get_subset(self, indices):
        """
        return a subset of client data and targets with the given indices
        """
        data_raw = [self.data[j] for j in indices]
        targets_raw = [int(self.targets[j]) for j in indices]
        # prepare data and targets for training
        data = torch.stack(data_raw, 0).to(device=self.device).to(torch.float32)
        targets = torch.tensor(targets_raw).to(device=self.device)
        return data, targets

    def split_indices(self, B):
        """
        return a list of indices for B batches
        """
        length = self.length
        indices = list(range(length))
        np.random.shuffle(indices)
        k = int(np.floor(length / B))
        # drops the last few datapoints, if needed, to keep batch size fixed
        return [indices[i : i + k] for i in range(0, len(indices), k)]


class Server:
    def __init__(self, model, val_data, val_targets, test_data, test_targets, device):
        self.model = deepcopy(model).to(device)
        self.val_data = val_data.to(device=device)
        self.val_targets = val_targets.to(device=device)
        self.test_data = test_data.to(device=device)
        self.test_targets = test_targets.to(device=device)
        self.length = len(test_data)
        self.device = device

    def aggregate(self, client_states, weights=None):
        """
        client_states - list of client states
        weights - weights for averaging (uniform by default)

        updates server model by performing weighted averaging
        """
        model = self.aggregate_(client_states, weights)
        self.model.load_state_dict(model.state_dict())

    def aggregate_(self, client_states, weights=None):
        """
        does not modify the server model
        only returns the updated model
        """
        if weights is None:
            # uniform weights by default
            weights = [1 / len(client_states)] * len(client_states)
        weights = np.array(weights)
        wtsum = np.sum(weights)
        weights = weights / wtsum  # normalize weights
        # initialise model parameters to zero
        model_state = self.model.state_dict()
        for key in model_state.keys():
            model_state[key] -= model_state[key]
        # find updated model - weighted averaging
        for idx, client_state in enumerate(client_states):
            for key in model_state.keys():
                model_state[key] += weights[idx] * client_state[key]
        model = deepcopy(self.model).to(device=self.device)
        model.load_state_dict(model_state)
        return model

    def shapley_values_mc(self, criterion, client_states, weights=None):
        """
        client_states - list of client states
        weights - weights for averaging (uniform by default)

        computes shapley values for the client updates on validation dataset
        """
        if weights is None:
            # uniform weights by default
            weights = [1 / len(client_states)] * len(client_states)
        weights = np.array(weights)
        wtsum = np.sum(weights)
        weights = weights / wtsum  # normalize weights

        num_clients = len(client_states)
        T = 50

        shapley_values = [[0] for i in range(num_clients)]

        for idx in range(num_clients):
            # compute shapley value of idx client
            """
            until convergence:
                sample a subset size k
                sample subset of size k of clients (except idx)
                compute updated model with this subset of clients
                compute loss of updated model on validation set
                compute another updated model with the idx client included
                compute loss of updated model on validation set
                compute difference between losses of the two models
                average losses over subsets to compute the shapley value of idx client
            """
            t = 0
            remaining_clients = [i for i in range(num_clients) if i != idx]
            while t < T:
                subset_size = np.random.choice(list(range(num_clients - 1)), size=1)[0]
                subset = np.random.choice(
                    remaining_clients, size=subset_size, replace=False
                )
                client_states_subset = [client_states[i] for i in subset]
                weights_subset = [weights[i] for i in subset]
                model_subset = self.aggregate_(client_states_subset, weights_subset)
                loss_subset = self.val_loss(model_subset, criterion)

                client_states_subset.append(client_states[idx])
                weights_subset.append(weights[idx])
                model_subset_with_idx = self.aggregate_(
                    client_states_subset, weights_subset
                )
                loss_subset_with_idx = self.val_loss(model_subset_with_idx, criterion)

                loss_diff = loss_subset - loss_subset_with_idx
                prev_avg = shapley_values[idx][-1]
                new_avg = (t * prev_avg + loss_diff) / (t + 1)
                shapley_values[idx].append(new_avg)
                if convergenceTest(shapley_values[idx]):
                    break
                t += 1
        final_shapley_values = [shapley_values[i][-1] for i in range(num_clients)]
        return final_shapley_values

    def shapley_values_tmc(self, criterion, client_states, weights=None):
        """
        client_states - list of client states
        weights - weights for averaging (uniform by default)

        computes shapley values for the client updates on validation dataset
        """
        if weights is None:
            # uniform weights by default
            weights = [1 / len(client_states)] * len(client_states)
        weights = np.array(weights)
        wtsum = np.sum(weights)
        weights = weights / wtsum  # normalize weights

        num_clients = len(client_states)

        shapley_values = [[0] for i in range(num_clients)]
        converged = False

        T = 50 * num_clients
        t = 0
        threshold = 1e-5
        v_init = self.val_loss(self.model, criterion)  # initial server model loss
        model_final = self.aggregate_(client_states, weights)
        v_final = self.val_loss(model_final, criterion)  # final server model loss
        while not converged and (t < T):
            t += 1
            client_permutation = np.random.permutation(num_clients)
            v_j = v_init
            for j in range(num_clients):
                if np.abs(v_final - v_j) < threshold:
                    v_jplus1 = v_j
                else:
                    subset = client_permutation[: (j + 1)]
                    client_states_subset = [client_states[i] for i in subset]
                    weights_subset = [weights[i] for i in subset]
                    model_subset = self.aggregate_(client_states_subset, weights_subset)
                    v_jplus1 = self.val_loss(model_subset, criterion)

                phi_old = shapley_values[client_permutation[j]][-1]
                phi_new = ((t - 1) * phi_old + (v_jplus1 - v_j)) / t
                shapley_values[client_permutation[j]].append(phi_new)

            flag = True
            for j in range(num_clients):
                if not convergenceTest(shapley_values):
                    flag = False
            if flag:
                converged = True

        final_shapley_values = [shapley_values[i][-1] for i in range(num_clients)]
        return final_shapley_values

    def shapley_values_true(self, criterion, client_states, weights=None):
        """
        client_states - list of client states
        weights - weights for averaging (uniform by default)

        computes shapley values for the client updates on validation dataset
        """

        def powerset(iterable):
            "powerset([1,2,3]) --> () (1,) (2,) (3,) (1,2) (1,3) (2,3) (1,2,3)"
            s = list(iterable)
            return list(
                chain.from_iterable(combinations(s, r) for r in range(len(s) + 1))
            )

        if weights is None:
            # uniform weights by default
            weights = [1 / len(client_states)] * len(client_states)
        weights = np.array(weights)
        wtsum = np.sum(weights)
        weights = weights / wtsum  # normalize weights

        num_clients = len(client_states)
        client_subsets = powerset(range(num_clients))
        subset_losses = {i: 0 for i in client_subsets}
        shapley_values = [[0] for i in range(num_clients)]

        for subset in client_subsets:
            client_states_subset = [client_states[i] for i in subset]
            weights_subset = [weights[i] for i in subset]
            model_subset = self.aggregate_(client_states_subset, weights_subset)
            loss_subset = self.val_loss(model_subset, criterion)
            subset_losses[subset] = loss_subset

        for subset in client_subsets:
            for idx in range(num_clients):
                L = len(subset)  # subset size
                if idx in subset:
                    nck = comb(num_clients - 1, L - 1)
                    prev_val = shapley_values[idx][-1]
                    new_val = prev_val + subset_losses[subset] / nck
                    shapley_values[idx].append(new_val)
                else:
                    nck = comb(num_clients - 1, L)
                    prev_val = shapley_values[idx][-1]
                    new_val = prev_val - subset_losses[subset] / nck
                    shapley_values[idx].append(new_val)

        final_shapley_values = [shapley_values[i][-1] for i in range(num_clients)]
        return final_shapley_values

    def test_loss(self, criterion):
        """
        criterion - loss function (model, data, targets)

        computes loss on test set with the server model
        """
        self.model.eval()
        with torch.no_grad():
            loss = criterion(self.model, self.test_data, self.test_targets)
        self.model.train()
        return float(loss.cpu())

    def accuracy(self):
        """
        test accuracy
        """
        self.model.eval()
        with torch.no_grad():
            scores = self.model(self.test_data)
            _, predictions = scores.max(1)
            num_correct = torch.sum(predictions == self.test_targets)
            total = self.length
            accuracy = num_correct / total
        self.model.train()
        return float(accuracy.cpu())

    def val_loss(self, model, criterion):
        """
        model
        criterion - loss function (model, data, targets)

        computes loss on validation set with the given model
        """
        model.eval()
        with torch.no_grad():
            loss = criterion(model, self.val_data, self.val_targets)
        model.train()
        return float(loss.cpu())


# data, models
"""
1. Synthetic(alpha, beta) - Logistic Regression
    num_clients = 30
    number of data points distributed by power law
    
2. MNIST - MLP

3. CIFAR10 - CNN

"""


def initNetworkData(dataset, num_clients, random_seed, alpha, beta=0):
    """
    choose dataset from ["synthetic", "mnist", "cifar10"]
    num_clients - number of clients
    random_seed - random seed
    alpha - Dirichlet parameter (for mnist, cifar10) / Variance (for synthetic)
    beta - Variance parameter (for synthetic only, not needed for mnist, cifar10)
    """
    torch.manual_seed(random_seed)
    np.random.seed(random_seed)
    if dataset not in ["synthetic", "mnist", "cifar10"]:
        raise Exception("Invalid dataset")

    elif dataset == "synthetic":
        clients = []
        test_val_data = []
        test_val_targets = []

        # distribute data points to num_clients clients by the power law
        client_datapoint_fractions = np.random.uniform(0, 1, num_clients) ** (
            1 / 3
        )  # inverse CDF sampling
        client_datapoint_fractions = client_datapoint_fractions / np.sum(
            client_datapoint_fractions
        )
        total_train_datapoints = 60000
        num_datapoints = total_train_datapoints * client_datapoint_fractions
        for i in range(num_clients):
            N_i = int(num_datapoints[i])
            train_i, test_val_i = synthetic_samples(alpha, beta, N_i)
            clients.append(Client(train_i["data"], train_i["targets"], device))
            test_val_data.extend(test_val_i["data"])
            test_val_targets.extend(test_val_i["targets"])

        serverModel = nn.Sequential(nn.Linear(60, 10))
        # compute total number of datapoints in test_val_data
        test_val_length = len(test_val_data)
        # split these 50:50 between test and val sets
        test_val_indices = list(range(test_val_length))
        np.random.shuffle(test_val_indices)
        test_indices = test_val_indices[: int(test_val_length / 2)]
        val_indices = test_val_indices[int(test_val_length / 2) :]
        test_val_data = torch.stack(test_val_data)
        test_val_targets = torch.stack(test_val_targets)
        val_data = test_val_data[val_indices]
        val_targets = test_val_targets[val_indices]
        test_data = test_val_data[test_indices]
        test_targets = test_val_targets[test_indices]
        server = Server(
            serverModel, val_data, val_targets, test_data, test_targets, device
        )

    elif dataset == "mnist":
        train_dataset, val_dataset, test_dataset = load_mnist_flat()
        client_indices = NIIDClientSplit(train_dataset, num_clients, alpha)
        clients = []
        for i in range(num_clients):
            clients.append(
                Client(
                    train_dataset.data[client_indices[i]],
                    train_dataset.targets[client_indices[i]],
                    device,
                )
            )

        serverModel = NN(input_dim=784, output_dim=10)
        server = Server(
            serverModel,
            val_dataset.data,
            val_dataset.targets,
            test_dataset.data,
            test_dataset.targets,
            device,
        )

    elif dataset == "cifar10":
        train_dataset, val_dataset, test_dataset = load_cifar10()
        client_indices = NIIDClientSplit(train_dataset, num_clients, alpha)
        clients = []
        for i in range(num_clients):
            clients.append(
                Client(
                    train_dataset.data[client_indices[i]],
                    train_dataset.targets[client_indices[i]],
                    device,
                )
            )
        in_channels = 3
        output_dim = 10
        input_h = 32
        input_w = 32
        serverModel = CNN(in_channels, input_w, input_h, output_dim)
        server = Server(
            serverModel,
            val_dataset.data,
            val_dataset.targets,
            test_dataset.data,
            test_dataset.targets,
            device,
        )

    return clients, server


"""
main code starts here
"""

# CONFIGURE

# generate/load data and distribute across clients and server
# datasets = ["cifar10", "mnist", "synthetic"]

dataset = "cifar10"
num_clients = 40
random_seed = 2
alpha = 1e6 
beta = 1  # needed for synthetic dataset

clients, server = initNetworkData(dataset, num_clients, random_seed, alpha, beta)
wandb_config["dataset"] = dataset
wandb_config["num_clients"] = num_clients
wandb_config["alpha"] = alpha
wandb_config["beta"] = beta
wandb_config["clients"] = clients
wandb_config["server"] = server

T = 100  # number of communications rounds
select_fraction = 0.1
wandb_config["num_communication_rounds"] = T
wandb_config["select_fraction"] = select_fraction
random_seed = 1


# both may be same/different
## define a criterion for client optimisation
## define a criterion for server evaluation


def fed_prox_criterion(model_reference, mu):
    """
    returns the required function when called
    loss function for FedProx with chosen mu parameter
    """
    model_reference = deepcopy(model_reference)

    def loss(model, data, targets):
        criterion = torch.nn.CrossEntropyLoss()
        scores = model(data)
        loss_value = criterion(scores, targets)
        for param, param_reference in zip(
            model.parameters(), model_reference.parameters()
        ):
            loss_value += 0.5 * mu * (param - param_reference).norm(2)
        return loss_value

    return loss


def fed_avg_criterion():
    def loss(model, data, targets):
        criterion = torch.nn.CrossEntropyLoss()
        scores = model(data)
        return criterion(scores, targets)

    return loss


def topk(values, k):
    # returns indices of top-k values with ties broken at random
    values = np.array(values)
    p = np.random.permutation(len(values))
    indices = p[np.argpartition(values[p], -k)[-k:]]
    return indices


def fed_avg_run(
    clients,
    server,
    select_fraction,
    T,
    random_seed=0,
    E=5,
    B=10,
    learning_rate=0.01,
    momentum=0.5,
):
    config = deepcopy(wandb_config)
    config["algorithm"] = "FedAvg"
    wandb.init(project="federated-learning-summary", config=config)
    clients = deepcopy(clients)
    server = deepcopy(server)
    torch.manual_seed(random_seed)
    np.random.seed(random_seed)
    num_clients = len(clients)
    num_selected = int(np.ceil(select_fraction * num_clients))

    accuracy = []
    val_loss = []
    test_loss = []
    for t in tqdm(range(T)):
        # select clients to transmit weights to

        # uniform random
        all_clients = [i for i in range(num_clients)]
        np.random.shuffle(all_clients)
        selected_client_indices = all_clients[0:num_selected]
        selected_status = [False for i in range(num_clients)]
        for i in range(num_clients):
            if i in selected_client_indices:
                selected_status[i] = True

        client_states = []
        weights = []
        for idx, client in enumerate(clients):
            if selected_status[idx]:
                # perform descent at client
                client_state = client.train(
                    server.model,
                    criterion=fed_avg_criterion(),
                    E=E,
                    B=B,
                    learning_rate=learning_rate,
                    momentum=momentum,
                )
                weight = client.length  # number of data points at client
                client_states.append(client_state)
                weights.append(weight)

        server.aggregate(client_states, weights)
        accuracy_now = server.accuracy()
        val_loss_now = server.val_loss(server.model, fed_avg_criterion())
        test_loss_now = server.test_loss(fed_avg_criterion())
        accuracy.append(accuracy_now)
        val_loss.append(val_loss_now)
        test_loss.append(test_loss_now)

        log_dict = {
            "accuracy": accuracy_now,
            "val_loss": val_loss_now,
            "test_loss": test_loss_now,
        }
        wandb.log(log_dict)

    wandb.finish()
    return accuracy, val_loss, test_loss


def fed_prox_run(
    clients,
    server,
    select_fraction,
    T,
    mu,
    random_seed=0,
    E=5,
    B=10,
    learning_rate=0.01,
    momentum=0.5,
):
    config = deepcopy(wandb_config)
    config["algorithm"] = "FedProx"
    config["mu"] = mu
    wandb.init(project="federated-learning-summary", config=config)

    clients = deepcopy(clients)
    server = deepcopy(server)
    torch.manual_seed(random_seed)
    np.random.seed(random_seed)
    num_clients = len(clients)
    num_selected = int(np.ceil(select_fraction * num_clients))

    accuracy = []
    val_loss = []
    test_loss = []
    for t in tqdm(range(T)):
        # select clients to transmit weights to

        # uniform random
        all_clients = [i for i in range(num_clients)]
        np.random.shuffle(all_clients)
        selected_client_indices = all_clients[0:num_selected]
        selected_status = [False for i in range(num_clients)]
        for i in range(num_clients):
            if i in selected_client_indices:
                selected_status[i] = True

        client_states = []
        weights = []
        for idx, client in enumerate(clients):
            if selected_status[idx]:
                # perform descent at client
                client_state = client.train(
                    server.model,
                    criterion=fed_prox_criterion(server.model, mu=mu),
                    E=E,
                    B=B,
                    learning_rate=learning_rate,
                    momentum=momentum,
                )
                weight = client.length  # number of data points at client
                client_states.append(client_state)
                weights.append(weight)

        server.aggregate(client_states, weights)
        accuracy_now = server.accuracy()
        val_loss_now = server.val_loss(server.model, fed_avg_criterion())
        test_loss_now = server.test_loss(fed_avg_criterion())
        accuracy.append(accuracy_now)
        val_loss.append(val_loss_now)
        test_loss.append(test_loss_now)

        log_dict = {
            "accuracy": accuracy_now,
            "val_loss": val_loss_now,
            "test_loss": test_loss_now,
        }
        wandb.log(log_dict)

    wandb.finish()

    return accuracy, val_loss, test_loss


def power_of_choice_run(
    clients,
    server,
    select_fraction,
    T,
    decay_factor=1,
    random_seed=0,
    E=5,
    B=10,
    learning_rate=0.01,
    momentum=0.5,
):
    """
    Power of Choice
    decay_factor (default = 1, no decay)
        determines the decay rate of number of clients to transmit the server model to (choose_from)
    """
    config = deepcopy(wandb_config)
    config["algorithm"] = "Power of Choice"
    config["decay_factor"] = decay_factor
    wandb.init(project="federated-learning-summary", config=config)
    clients = deepcopy(clients)
    server = deepcopy(server)
    torch.manual_seed(random_seed)
    np.random.seed(random_seed)
    num_clients = len(clients)
    num_selected = int(np.ceil(select_fraction * num_clients))
    choose_from = num_clients  # the size of initial client subset to query for loss

    accuracy = []
    val_loss = []
    test_loss = []
    for t in tqdm(range(T)):
        # select clients to transmit weights to
        if choose_from > num_selected:
            choose_from *= decay_factor
            choose_from = int(np.ceil(choose_from))
        # uniform random
        all_clients = [i for i in range(num_clients)]
        np.random.shuffle(all_clients)
        selected_client_indices = all_clients[0:choose_from]
        selected_status = [False for i in range(num_clients)]
        for i in range(num_clients):
            if i in selected_client_indices:
                selected_status[i] = True

        client_losses = []  # will store array of size choose_from
        for idx, client in enumerate(clients):
            if selected_status[idx]:
                # query selected clients for loss
                client_loss = client.loss(server.model, fed_avg_criterion())
                client_losses.append(client_loss)
        # find indices of largest num_selected values in client_losses
        indices = topk(client_losses, num_selected)
        selected_client_indices_2 = []  # will store array of size num_selected
        for i in indices:
            selected_client_indices_2.append(selected_client_indices[i])

        selected_status = [False for i in range(num_clients)]
        for i in range(num_clients):
            if i in selected_client_indices_2:
                selected_status[i] = True

        client_states = []
        weights = []
        for idx, client in enumerate(clients):
            if selected_status[idx]:
                # perform descent at client
                client_state = client.train(
                    server.model,
                    criterion=fed_avg_criterion(),
                    E=E,
                    B=B,
                    learning_rate=learning_rate,
                    momentum=momentum,
                )
                weight = client.length  # number of data points at client
                client_states.append(client_state)
                weights.append(weight)

        server.aggregate(client_states, weights)
        accuracy_now = server.accuracy()
        val_loss_now = server.val_loss(server.model, fed_avg_criterion())
        test_loss_now = server.test_loss(fed_avg_criterion())
        accuracy.append(accuracy_now)
        val_loss.append(val_loss_now)
        test_loss.append(test_loss_now)

        log_dict = {
            "accuracy": accuracy_now,
            "val_loss": val_loss_now,
            "test_loss": test_loss_now,
        }
        wandb.log(log_dict)

    wandb.finish()

    return accuracy, val_loss, test_loss


def shapley_run(
    clients,
    server,
    select_fraction,
    T,
    client_selection,
    random_seed=0,
    E=5,
    B=10,
    learning_rate=0.01,
    momentum=0.5,
):
    config = deepcopy(wandb_config)
    config["client_selection"] = client_selection
    wandb.init(project="federated-learning-summary", config=config)
    clients = deepcopy(clients)
    server = deepcopy(server)
    torch.manual_seed(random_seed)
    np.random.seed(random_seed)
    num_clients = len(clients)
    num_selected = int(np.ceil(select_fraction * num_clients))
    choose_from = num_clients  # the size of initial client subset to query for loss

    accuracy = []
    val_loss = []
    test_loss = []
    shapley_values_T = []
    selections_T = []
    for t in tqdm(range(T)):
        # select clients to transmit weights to
        # uniform random
        client_states = []
        weights = []
        client_losses = []
        for idx, client in enumerate(clients):
            client_losses.append(client.loss(server.model, fed_avg_criterion()))
            # perform descent at client
            client_state = client.train(
                server.model,
                criterion=fed_avg_criterion(),
                E=E,
                B=B,
                learning_rate=learning_rate,
                momentum=momentum,
            )
            weight = client.length  # number of data points at client
            client_states.append(client_state)
            weights.append(weight)
        # compute shapley values for each client
        # shapley_values = server.shapley_values_mc(
        #     fed_avg_criterion(), client_states, weights
        # )
        # shapley_values = server.shapley_values_tmc(
        #     fed_avg_criterion(), client_states, weights
        # )
        shapley_values = server.shapley_values_true(
            fed_avg_criterion(), client_states, weights
        )
        shapley_values_T.append(shapley_values)

        # find indices of largest num_selected values in shapley_values
        selections = [0 for i in range(num_clients)]
        if client_selection == "best":
            indices = topk(shapley_values, num_selected)
        elif client_selection == "fedavg":
            indices = np.random.choice(num_clients, size=num_selected, replace=False)
        elif client_selection == "worst":
            indices = np.argpartition(shapley_values, num_selected)[:num_selected]
        elif client_selection == "power_of_choice":
            indices = topk(client_losses, num_selected)
        client_states_chosen = [client_states[i] for i in indices]
        weights_chosen = [weights[i] for i in indices]

        for idx in indices:
            selections[idx] = 1
        selections_T.append(selections)

        server.aggregate(client_states_chosen, weights_chosen)
        accuracy_now = server.accuracy()
        val_loss_now = server.val_loss(server.model, fed_avg_criterion())
        test_loss_now = server.test_loss(fed_avg_criterion())
        accuracy.append(accuracy_now)
        val_loss.append(val_loss_now)
        test_loss.append(test_loss_now)

        log_dict = {
            "accuracy": accuracy_now,
            "val_loss": val_loss_now,
            "test_loss": test_loss_now,
        }
        for i in range(num_clients):
            log_dict[f"shapley_value_{i}"] = shapley_values[i]
            log_dict[f"selection_{i}"] = selections[i]
        wandb.log(log_dict)

    wandb.finish()
    return accuracy, val_loss, test_loss, shapley_values_T, selections_T


def ucb_run(
    clients,
    server,
    select_fraction,
    T,
    beta,
    random_seed=0,
    E=5,
    B=10,
    learning_rate=0.01,
    momentum=0.5,
):
    config = deepcopy(wandb_config)
    config["client_selection"] = "ucb"
    config["beta"] = beta
    wandb.init(project="federated-learning-summary", config=config)
    clients = deepcopy(clients)
    server = deepcopy(server)
    torch.manual_seed(random_seed)
    np.random.seed(random_seed)
    num_clients = len(clients)
    num_selected = int(np.ceil(select_fraction * num_clients))

    accuracy = []
    val_loss = []
    test_loss = []
    shapley_values_T = []
    selections_T = []
    draws_T = []

    N_t = [0 for i in range(num_clients)]
    UCB = [0 for i in range(num_clients)]
    SV = [0 for i in range(num_clients)]
    for t in tqdm(range(T)):
        # select clients to transmit weights to
        # initially sample every client atleast once
        selected_status = [False for i in range(num_clients)]
        if t < np.floor(num_clients / num_selected):
            for idx in range(t * num_selected, (t + 1) * num_selected):
                selected_status[idx] = True
                N_t[idx] += 1
        elif t == np.floor(num_clients / num_selected):
            for idx in range(t * num_selected, num_clients):
                selected_status[idx] = True
                N_t[idx] += 1
            remaining_selections = num_selected * (t + 1) - num_clients
            if remaining_selections > 0:
                unselected_indices = list(range(0, t * num_selected))
                selected_indices_subset = np.random.choice(
                    unselected_indices, size=remaining_selections, replace=False
                )
                for idx in selected_indices_subset:
                    selected_status[idx] = True
                    N_t[idx] += 1
        else:
            # do UCB selection
            selected_indices = topk(UCB, num_selected)
            for idx in selected_indices:
                selected_status[idx] = True
                N_t[idx] += 1
        # uniform random
        client_states = []
        weights = []

        for idx, client in enumerate(clients):
            if selected_status[idx]:
                # perform descent at client
                client_state = client.train(
                    server.model,
                    criterion=fed_avg_criterion(),
                    E=E,
                    B=B,
                    learning_rate=learning_rate,
                    momentum=momentum,
                )
                weight = client.length  # number of data points at client
                client_states.append(client_state)
                weights.append(weight)

        # compute shapley values for each client BEFORE updating server model
        # shapley_values = server.shapley_values_mc(
        #     fed_avg_criterion(), client_states, weights
        # )
        # shapley_values = server.shapley_values_tmc(
        #     fed_avg_criterion(), client_states, weights
        # )
        shapley_values = server.shapley_values_true(
            fed_avg_criterion(), client_states, weights
        )
        # update server model
        server.aggregate(client_states, weights)
        accuracy_now = server.accuracy()
        val_loss_now = server.val_loss(server.model, fed_avg_criterion())
        test_loss_now = server.test_loss(fed_avg_criterion())
        accuracy.append(accuracy_now)
        val_loss.append(val_loss_now)
        test_loss.append(test_loss_now)

        log_dict = {
            "accuracy": accuracy_now,
            "val_loss": val_loss_now,
            "test_loss": test_loss_now,
        }

        # compute UCB for next round of selections
        selections = [0 for i in range(num_clients)]
        counter = 0
        for i in range(num_clients):
            if selected_status[i]:
                SV[i] = ((N_t[i] - 1) * SV[i] + shapley_values[counter]) / N_t[i]
                counter += 1
                selections[i] = 1
            UCB[i] = SV[i] + beta * np.sqrt(np.log(t + 1) / N_t[i])
        shapley_values_T.append(deepcopy(SV))
        selections_T.append(deepcopy(selections))
        draws_T.append(deepcopy(N_t))

        for i in range(num_clients):
            log_dict[f"shapley_value_{i}"] = SV[i]
            log_dict[f"selection_{i}"] = selections[i]
        wandb.log(log_dict)

    wandb.finish()
    return accuracy, val_loss, test_loss, shapley_values_T, selections_T, draws_T


def sfedavg_run(
    clients,
    server,
    select_fraction,
    T,
    alpha,
    beta,
    random_seed=0,
    E=5,
    B=10,
    learning_rate=0.01,
    momentum=0.5,
):
    config = deepcopy(wandb_config)
    config["client_selection"] = "S-FedAvg"
    config["algo-alpha"] = alpha
    config["algo-beta"] = beta
    wandb.init(project="federated-learning-summary", config=config)
    clients = deepcopy(clients)
    server = deepcopy(server)
    torch.manual_seed(random_seed)
    np.random.seed(random_seed)
    num_clients = len(clients)
    num_selected = int(np.ceil(select_fraction * num_clients))

    accuracy = []
    val_loss = []
    test_loss = []
    shapley_values_T = []
    selections_T = []
    Phi_T = []
    draws_T = []

    N_t = [0 for i in range(num_clients)]
    Phi = [1 / (num_clients) for i in range(num_clients)]
    SV = [0 for i in range(num_clients)]
    for t in tqdm(range(T)):
        # select clients to transmit weights to
        # initially sample every client atleast once
        selected_status = [False for i in range(num_clients)]
        # do Game of Gradients Selection
        all_indices = list(range(num_clients))
        probs = np.exp(np.array(Phi))
        probs = probs / np.sum(probs)
        selected_indices = np.random.choice(
            all_indices, size=num_selected, replace=False, p=probs
        )
        for idx in selected_indices:
            selected_status[idx] = True
            N_t[idx] += 1
        # uniform random
        client_states = []
        weights = []

        for idx, client in enumerate(clients):
            if selected_status[idx]:
                # perform descent at client
                client_state = client.train(
                    server.model,
                    criterion=fed_avg_criterion(),
                    E=E,
                    B=B,
                    learning_rate=learning_rate,
                    momentum=momentum,
                )
                weight = client.length  # number of data points at client
                weight /= probs[idx]  # for unbiased averaging
                client_states.append(client_state)
                weights.append(weight)

        # compute shapley values for each client BEFORE updating server model
        # shapley_values = server.shapley_values_mc(
        #     fed_avg_criterion(), client_states, weights
        # )
        # shapley_values = server.shapley_values_tmc(
        #     fed_avg_criterion(), client_states, weights
        # )
        shapley_values = server.shapley_values_true(
            fed_avg_criterion(), client_states, weights
        )
        # update server model
        server.aggregate(client_states, weights)
        accuracy_now = server.accuracy()
        val_loss_now = server.val_loss(server.model, fed_avg_criterion())
        test_loss_now = server.test_loss(fed_avg_criterion())
        accuracy.append(accuracy_now)
        val_loss.append(val_loss_now)
        test_loss.append(test_loss_now)

        log_dict = {
            "accuracy": accuracy_now,
            "val_loss": val_loss_now,
            "test_loss": test_loss_now,
        }

        # compute Phi for next round of selections
        selections = [0 for i in range(num_clients)]
        counter = 0
        # defined as function parameters now
        # alpha = 0.75
        # beta = 0.25
        for i in range(num_clients):
            if selected_status[i]:
                SV[i] = ((N_t[i] - 1) * SV[i] + shapley_values[counter]) / N_t[i]
                counter += 1
                selections[i] = 1
                Phi[i] = alpha * Phi[i] + beta * SV[i]
        shapley_values_T.append(deepcopy(SV))
        Phi_T.append(deepcopy(Phi))
        selections_T.append(deepcopy(selections))
        draws_T.append(deepcopy(N_t))

        for i in range(num_clients):
            log_dict[f"shapley_value_{i}"] = SV[i]
            log_dict[f"selection_{i}"] = selections[i]
        wandb.log(log_dict)

    wandb.finish()
    return accuracy, val_loss, test_loss, shapley_values_T, Phi_T, selections_T, draws_T


def ucb_runs(beta, runs):
    avg_accuracy_list = []
    for run in range(runs):
        accuracy_list, *_ = ucb_run(
            deepcopy(clients),
            deepcopy(server),
            select_fraction,
            T,
            beta=beta,
            random_seed=run,
        )
        accuracy_list = np.array(accuracy_list)
        if run == 0:
            avg_accuracy_list = deepcopy(accuracy_list)
        avg_accuracy_list = (run * avg_accuracy_list + accuracy_list) / (run + 1)
    return avg_accuracy_list


def sfedavg_runs(alpha, beta, runs):
    avg_accuracy_list = []
    for run in range(runs):
        accuracy_list, *_ = sfedavg_run(
            deepcopy(clients),
            deepcopy(server),
            select_fraction,
            T,
            alpha=alpha,
            beta=beta,
            random_seed=run,
        )

        accuracy_list = np.array(accuracy_list)
        if run == 0:
            avg_accuracy_list = deepcopy(accuracy_list)
        avg_accuracy_list = (run * avg_accuracy_list + accuracy_list) / (run + 1)
    return avg_accuracy_list


def fedavg_runs(runs):
    avg_accuracy_list = []
    for run in range(runs):
        accuracy_list, *_ = fed_avg_run(
            deepcopy(clients),
            deepcopy(server),
            select_fraction,
            T,
            random_seed=run,
        )
        accuracy_list = np.array(accuracy_list)
        if run == 0:
            avg_accuracy_list = deepcopy(accuracy_list)
        avg_accuracy_list = (run * avg_accuracy_list + accuracy_list) / (run + 1)
    return avg_accuracy_list


def poc_runs(decay_factor, runs):
    avg_accuracy_list = []
    for run in range(runs):
        accuracy_list, *_ = power_of_choice_run(
            deepcopy(clients),
            deepcopy(server),
            select_fraction,
            T,
            decay_factor=decay_factor,
            random_seed=random_seed,
        )
        accuracy_list = np.array(accuracy_list)
        if run == 0:
            avg_accuracy_list = deepcopy(accuracy_list)
        avg_accuracy_list = (run * avg_accuracy_list + accuracy_list) / (run + 1)
    return avg_accuracy_list


def fedprox_runs(mu, runs):
    avg_accuracy_list = []
    for run in range(runs):
        accuracy_list, *_ = fed_prox_run(
            deepcopy(clients),
            deepcopy(server),
            select_fraction,
            T,
            mu=mu,
            random_seed=random_seed,
        )
        accuracy_list = np.array(accuracy_list)
        if run == 0:
            avg_accuracy_list = deepcopy(accuracy_list)
        avg_accuracy_list = (run * avg_accuracy_list + accuracy_list) / (run + 1)
    return avg_accuracy_list


dirichlet_alpha = alpha
synthetic_alpha = alpha
synthetic_beta = beta

# UCB search

beta_vals = [1e-1, 1, 1e1, 1e2]
accuracies_ucb = {}
for beta in beta_vals:
    accuracies_ucb[beta] = ucb_runs(beta, 3)

method = "ucb"
accuracies_summary = accuracies_ucb

if dataset in ["mnist", "cifar10"]:
    with open(
        f"./results/{method}_{dataset}_{num_clients}_{random_seed}_{dirichlet_alpha}.pickle",
        "wb",
    ) as f:
        pickle.dump(accuracies_summary, f)
else:
    with open(
        f"./results/{method}_{dataset}_{num_clients}_{random_seed}_{synthetic_alpha}_{synthetic_beta}.pickle",
        "wb",
    ) as f:
        pickle.dump(accuracies_summary, f)


# S-FedAvg search

alpha_vals = np.arange(0.1, 1, 0.2)
beta_vals = np.arange(0.1, 1, 0.2)
accuracies_sfedavg = {}
for alpha in alpha_vals:
    beta = 1 - alpha
    accuracies_sfedavg[(alpha, beta)] = sfedavg_runs(alpha, beta, 3)

method = "sfedavg"
accuracies_summary = accuracies_sfedavg

if dataset in ["mnist", "cifar10"]:
    with open(
        f"./results/{method}_{dataset}_{num_clients}_{random_seed}_{dirichlet_alpha}.pickle",
        "wb",
    ) as f:
        pickle.dump(accuracies_summary, f)
else:
    with open(
        f"./results/{method}_{dataset}_{num_clients}_{random_seed}_{synthetic_alpha}_{synthetic_beta}.pickle",
        "wb",
    ) as f:
        pickle.dump(accuracies_summary, f)

# FedAvg

accuracies_fedavg = fedavg_runs(5)
method = "fedavg"
accuracies_summary = accuracies_fedavg

if dataset in ["mnist", "cifar10"]:
    with open(
        f"./results/{method}_{dataset}_{num_clients}_{random_seed}_{dirichlet_alpha}.pickle",
        "wb",
    ) as f:
        pickle.dump(accuracies_summary, f)
else:
    with open(
        f"./results/{method}_{dataset}_{num_clients}_{random_seed}_{synthetic_alpha}_{synthetic_beta}.pickle",
        "wb",
    ) as f:
        pickle.dump(accuracies_summary, f)

# Power-of-Choice

decay_factors = [1, 0.99, 0.95, 0.9, 0.8]
accuracies_poc = {}
for decay_factor in decay_factors:
    accuracies_poc[decay_factor] = poc_runs(decay_factor, 3)

method = "poc"
accuracies_summary = accuracies_poc

if dataset in ["mnist", "cifar10"]:
    with open(
        f"./results/{method}_{dataset}_{num_clients}_{random_seed}_{dirichlet_alpha}.pickle",
        "wb",
    ) as f:
        pickle.dump(accuracies_summary, f)
else:
    with open(
        f"./results/{method}_{dataset}_{num_clients}_{random_seed}_{synthetic_alpha}_{synthetic_beta}.pickle",
        "wb",
    ) as f:
        pickle.dump(accuracies_summary, f)

# FedProx

mu_vals = [10**i for i in range(-3, 3)]
accuracies_fedprox = {}
for mu in mu_vals:
    accuracies_fedprox[mu] = fedprox_runs(mu, 3)

method = "fedprox"
accuracies_summary = accuracies_fedprox

if dataset in ["mnist", "cifar10"]:
    with open(
        f"./results/{method}_{dataset}_{num_clients}_{random_seed}_{dirichlet_alpha}.pickle",
        "wb",
    ) as f:
        pickle.dump(accuracies_summary, f)
else:
    with open(
        f"./results/{method}_{dataset}_{num_clients}_{random_seed}_{synthetic_alpha}_{synthetic_beta}.pickle",
        "wb",
    ) as f:
        pickle.dump(accuracies_summary, f)


# sns.heatmap(draws_heatmap).set(title="draws")
# plt.show()
# sns.heatmap(shapley_heatmap).set(title="shapley values")
# plt.show()
# sns.heatmap(selection_heatmap).set(title="selections")
# plt.show()

# plt.plot(accuracy_ucb, label="UCB")
# plt.plot(accuracy_fedavg, label="fedavg")
# plt.legend()
# plt.show()

# for i in range(5):
#     accuracy_poc, _, _ = power_of_choice_run(
#         deepcopy(clients),
#         deepcopy(server),
#         select_fraction,
#         T,
#         decay_factor=0.95,
#         random_seed=i,
#     )

#     accuracy_poc_nodecay, _, _ = power_of_choice_run(
#         deepcopy(clients),
#         deepcopy(server),
#         select_fraction,
#         T,
#         decay_factor=1,
#         random_seed=i,
#     )

#     accuracy, _, _ = fed_avg_run(
#         deepcopy(clients), deepcopy(server), select_fraction, T, random_seed=i
#     )

#     accuracy_prox, _, _ = fed_prox_run(
#         deepcopy(clients), deepcopy(server), select_fraction, T, mu=0.1, random_seed=i
#     )
#     if i == 0:
#         accuracy_avg = np.array(accuracy)
#         accuracy_prox_avg = np.array(accuracy_prox)
#         accuracy_poc_avg = np.array(accuracy_poc)
#         accuracy_poc_nodecay_avg = np.array(accuracy_poc_nodecay)
#     else:
#         accuracy_avg = accuracy_avg * (i / (i + 1)) + np.array(accuracy) * (1 / (i + 1))
#         accuracy_prox_avg = accuracy_prox_avg * (i / (i + 1)) + np.array(
#             accuracy_prox
#         ) * (1 / (i + 1))
#         accuracy_poc_avg = accuracy_poc_avg * (i / (i + 1)) + np.array(accuracy_poc) * (
#             1 / (i + 1)
#         )
#         accuracy_poc_nodecay_avg = accuracy_poc_nodecay_avg * (i / (i + 1)) + np.array(
#             accuracy_poc_nodecay
#         ) * (1 / (i + 1))

# plt.plot(accuracy_avg, label="FedAvg")
# plt.plot(accuracy_prox_avg, label="FedProx")
# plt.plot(accuracy_poc_avg, label="Power of Choice")
# plt.plot(accuracy_poc_nodecay_avg, label="Power of Choice no decay")
# plt.legend()
# plt.show()
