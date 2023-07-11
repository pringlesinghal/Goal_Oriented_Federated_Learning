# -*- coding: utf-8 -*-
"""dropout_experiments.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/10ZbY-AMG4ceajp9E9jYT-vie0EP9FAcK
"""

# Commented out IPython magic to ensure Python compatibility.
# from google.colab import drive
# drive.mount('/content/gdrive')
# %mkdir gdrive/MyDrive/AAU_Project
# %cd gdrive/MyDrive/AAU_Project
# !git clone https://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@github.com/pringlesinghal/Goal_Oriented_Federated_Learning.git
# %cd Goal_Oriented_Federated_Learning/

# !pip install wandb -qU

# imports

import wandb

wandb.login()
import torch
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

import pickle
import os

from initialise import initNetworkData
from algorithms import (
    fed_avg_run,
    fed_prox_run,
    sfedavg_run,
    ucb_run,
    power_of_choice_run,
)
from utils import dict_hash

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class AlgoResults:
    def __init__(self, test_acc, train_acc, train_loss, val_loss, test_loss):
        self.test_acc = test_acc
        self.train_acc = train_acc
        self.train_loss = train_loss
        self.val_loss = val_loss
        self.test_loss = test_loss

    def get_results(self):
        return (
            self.test_acc,
            self.train_acc,
            self.train_loss,
            self.val_loss,
            self.test_loss,
        )

    def plot_accuracy(self):
        test_acc = self.test_acc
        train_acc = self.train_acc
        plt.plot(test_acc, label="test accuracy")
        plt.plot(train_acc, label="train accuracy")
        plt.legend()
        plt.show()

    def plot_loss(self):
        train_loss = self.train_loss
        val_loss = self.val_loss
        test_loss = self.test_loss
        plt.plot(train_loss, label="train loss")
        plt.plot(val_loss, label="val loss")
        plt.plot(test_loss, label="test loss")
        plt.legend()
        plt.show()

    def compute_sv_metrics(self):
        if self.config["algorithm"] == "ucb":
            cosine_distances_gtg = self.cosine_distance(
                self.sv_rounds["gtg"], self.sv_rounds["true"]
            )
            cosine_distances_tmc = self.cosine_distance(
                self.sv_rounds["tmc"], self.sv_rounds["true"]
            )
            self.cosine_distance_gtg = np.mean(cosine_distances_gtg)
            self.cosine_distance_tmc = np.mean(cosine_distances_tmc)
            self.num_evals_gtg = np.mean(np.array(self.num_model_evaluations["gtg"]))
            self.num_evals_tmc = np.mean(np.array(self.num_model_evaluations["tmc"]))
            self.num_evals_true = np.mean(np.array(self.num_model_evaluations["true"]))

            plt.plot(cosine_distances_gtg, label="cosine distance gtg")
            plt.plot(cosine_distances_tmc, label="cosine distance tmc")
            plt.legend()
            plt.show()

            for sv_method in ["gtg", "true", "tmc"]:
                plt.plot(
                    self.num_model_evaluations[sv_method],
                    label=f"{sv_method} numb model evals",
                )
            plt.legend()
            plt.show()

    def cosine_distance(self, sv_1, sv_2):
        num_sequences = len(sv_1)
        assert num_sequences == len(sv_2)
        cosine_distances = []
        for i in range(num_sequences):
            sv_1_norm = np.linalg.norm(np.array(sv_1[i]))
            sv_2_norm = np.linalg.norm(np.array(sv_2[i]))
            distance = 1 - np.dot(sv_1[i], sv_2[i]) / (sv_1_norm * sv_2_norm)
            cosine_distances.append(distance)
        return cosine_distances


class AlgoRun:
    def __init__(
        self,
        dataset_config,
        algorithm,
        select_fraction,
        algo_seed=0,
        data_seed=0,
        E=10,
        B=10,
        T=100,
        lr=0.01,
        momentum=0.5,
        mu=None,
        alpha=None,
        beta=None,
        decay_factor=None,
        noise_level=None,
    ):
        """
        dataset_config = dict with keys {dataset, num_clients, alpha, beta}
        """
        self.dataset_config = dataset_config
        self.data_seed = data_seed
        self.algorithm = algorithm
        self.select_fraction = select_fraction
        self.algo_seed = algo_seed
        # additional parameters
        self.E = E
        self.B = B
        self.T = T
        self.lr = lr
        self.momentum = momentum
        # algorithm parameters
        self.mu = mu
        self.alpha = alpha
        self.beta = beta
        self.decay_factor = decay_factor
        # noise parameters
        self.noise_level = noise_level
        if self.algorithm == "fedavg":
            self.mu = None
            self.alpha = None
            self.beta = None
            self.decay_factor = None
        elif self.algorithm == "fedprox":
            if mu is None:
                raise Exception("FedProx requires mu to be passed")
            self.mu = mu
            self.alpha = None
            self.beta = None
            self.decay_factor = None
        elif self.algorithm == "sfedavg":
            if (alpha is None) and (beta is None):
                raise Exception(
                    "S-FedAvg requires either alpha or beta to be specified"
                )
            elif alpha is None:
                self.beta = beta
                self.alpha = 1 - self.beta
            elif beta is None:
                self.alpha = alpha
                self.beta = 1 - self.alpha
            else:
                self.alpha = alpha
                self.beta = beta
                self.mu = None
                self.decay_factor = None
        elif algorithm == "poc":
            if decay_factor is None:
                raise Exception("poc requires decay_factor to be specified")
            self.decay_factor = decay_factor
            self.mu = None
            self.alpha = None
            self.beta = None
        elif algorithm == "ucb":
            if self.beta is None:
                raise Exception("ucb requires beta to be specified")
            self.beta = beta
            self.alpha = None
            self.mu = None
            self.decay_factor = None
        else:
            raise Exception("Unknown algorithm")

    def run(self, logging):
        """
        logging must be one of the following:
            1. False: no logging on wandb
            2. True: log the run on wandb
        """
        dataset_config = self.dataset_config
        data_seed = self.data_seed
        clients, server = initNetworkData(
            dataset=dataset_config["dataset"],
            num_clients=dataset_config["num_clients"],
            random_seed=data_seed,
            alpha=dataset_config["alpha"],
            beta=dataset_config["beta"],
            update_noise_level=self.noise_level,
        )
        algorithm = self.algorithm
        random_seed = self.algo_seed

        E = self.E
        B = self.B
        select_fraction = self.select_fraction
        T = self.T
        lr = self.lr
        momentum = self.momentum
        wandb_config = {
            "algorithm": self.algorithm,
            "dataset": self.dataset_config["dataset"],
            "num_clients": self.dataset_config["num_clients"],
            "dataset_alpha": self.dataset_config["alpha"],
            "dataset_beta": self.dataset_config["beta"],
            "algo_seed": self.algo_seed,
            "data_seed": self.data_seed,
            "E": E,
            "B": B,
            "select_fraction": select_fraction,
            "T": T,
            "lr": lr,
            "momentum": momentum,
            "mu": self.mu,
            "algo_alpha": self.alpha,
            "algo_beta": self.beta,
            "decay_factor": self.decay_factor,
        }

        if self.noise_level is not None:
            wandb_config["noise_level"] = self.noise_level

        # result_path = f'results/{self.dataset_config["dataset"]}/{self.algorithm}/{self.dataset_config["num_clients"]}-{int(self.select_fraction*self.dataset_config["num_clients"])}/'
        result_path = f'results-synthetic11/{self.algorithm}/select-{int(self.select_fraction*self.dataset_config["num_clients"])}/'
        if os.path.exists(result_path + f"{dict_hash(wandb_config)}.pickle"):
            print("this run has been performed earlier")
            with open(result_path + f"{dict_hash(wandb_config)}.pickle", "rb") as f:
                self.results = pickle.load(f)

            return self.results.get_results()

        if logging:
            wandb.init(project="FL-AAU-11-7", config=wandb_config)

        if algorithm == "fedavg":
            (
                test_acc,
                train_acc,
                train_loss,
                val_loss,
                test_loss,
                selections,
            ) = fed_avg_run(
                clients,
                server,
                select_fraction,
                T,
                random_seed=random_seed,
                E=E,
                B=B,
                learning_rate=lr,
                momentum=momentum,
                logging=logging,
            )
        elif algorithm == "fedprox":
            mu = self.mu
            (
                test_acc,
                train_acc,
                train_loss,
                val_loss,
                test_loss,
                selections,
            ) = fed_prox_run(
                clients,
                server,
                select_fraction,
                T,
                mu,
                random_seed=random_seed,
                E=E,
                B=B,
                learning_rate=lr,
                momentum=momentum,
                logging=logging,
            )
        elif algorithm == "sfedavg":
            alpha = self.alpha
            beta = self.beta
            (
                test_acc,
                train_acc,
                train_loss,
                val_loss,
                test_loss,
                selections,
                shapley_values,
            ) = sfedavg_run(
                clients,
                server,
                select_fraction,
                T,
                alpha,
                beta,
                random_seed=random_seed,
                E=E,
                B=B,
                learning_rate=lr,
                momentum=momentum,
                logging=logging,
            )
        elif algorithm == "ucb":
            beta = self.beta
            (
                test_acc,
                train_acc,
                train_loss,
                val_loss,
                test_loss,
                selections,
                shapley_values,
                sv_rounds,
                num_model_evaluations,
                ucb_values,
            ) = ucb_run(
                clients,
                server,
                select_fraction,
                T,
                beta,
                random_seed=random_seed,
                E=E,
                B=B,
                learning_rate=lr,
                momentum=momentum,
                logging=logging,
            )
        elif algorithm == "poc":
            decay_factor = self.decay_factor
            (
                test_acc,
                train_acc,
                train_loss,
                val_loss,
                test_loss,
                selections,
            ) = power_of_choice_run(
                clients,
                server,
                select_fraction,
                T,
                decay_factor=decay_factor,
                random_seed=random_seed,
                E=E,
                B=B,
                learning_rate=lr,
                momentum=momentum,
                logging=logging,
            )

        self.results = AlgoResults(test_acc, train_acc, train_loss, val_loss, test_loss)

        # log the selections for each algorithm
        self.results.selections = selections

        # for sfedavg and ucb
        #   log the SV
        #   also log the number of model evaluations for gtg, tmc, true as well as cosine distance between gtg|true, tmc|true
        if algorithm in ["sfedavg", "ucb"]:
            self.results.shapley_values = shapley_values
        if algorithm == "ucb":
            self.results.sv_rounds = sv_rounds
            self.results.num_model_evaluations = num_model_evaluations
            self.results.ucb_values = ucb_values
        if logging == True:
            self.results.config = wandb_config
            # result_path = f'results/{self.dataset_config["dataset"]}/{self.algorithm}/{self.dataset_config["num_clients"]}-{int(self.select_fraction*self.dataset_config["num_clients"])}/'
            result_path = f'results-synthetic11/{self.algorithm}/select-{int(self.select_fraction*self.dataset_config["num_clients"])}/'
            os.makedirs(result_path, exist_ok=True)
            with open(result_path + f"{dict_hash(wandb_config)}.pickle", "wb") as f:
                pickle.dump(self.results, f)
            # save results to local file
        return test_acc, train_acc, train_loss, val_loss, test_loss


def avg_runs(num_runs, algorun, logging):
    """
    Takes AlgoRun template algorun and performs multiple runs with different data and algorithm seeds
    """
    test_acc_avg = []
    train_acc_avg = []
    train_loss_avg = []
    val_loss_avg = []
    test_loss_avg = []

    for run in range(num_runs):
        algorun.data_seed = run
        algorun.algo_seed = run
        test_acc, train_acc, train_loss, val_loss, test_loss = algorun.run(logging)
        if run == 0:
            test_acc_avg = np.array(test_acc)
            train_acc_avg = np.array(train_acc)
            train_loss_avg = np.array(train_loss)
            val_loss_avg = np.array(val_loss)
            test_loss_avg = np.array(test_loss)
        else:
            test_acc_avg = (run * test_acc_avg + np.array(test_acc)) / (run + 1)
            train_acc_avg = (run * train_acc_avg + np.array(train_acc)) / (run + 1)
            train_loss_avg = (run * train_loss_avg + np.array(train_loss)) / (run + 1)
            val_loss_avg = (run * val_loss_avg + np.array(val_loss)) / (run + 1)
            test_loss_avg = (run * test_loss_avg + np.array(test_loss)) / (run + 1)
    return test_acc_avg, train_acc_avg, train_loss_avg, val_loss_avg, test_loss_avg


if __name__ == "__main__":
    """Experiments"""

    """
    Experiment 1
    """
    """
    First configure dataset and split
    """
    # dataset from ["cifar10", "mnist", "synthetic"]
    dataset = "cifar10"
    num_clients = 700
    dirichlet_alpha = 0.01
    dataset_alpha = 1
    dataset_beta = 1  # needed for synthetic dataset
    if dataset != "synthetic":
        dataset_alpha = dirichlet_alpha

    dataset_config = {
        "dataset": dataset,
        "num_clients": num_clients,
        "alpha": dataset_alpha,
        "beta": dataset_beta,
    }

    """
    Then configure the algorithm
    """
    algorithm = "fedavg"
    select_fraction = 10 / 700

    E = 10
    B = 10
    T = 400
    lr = 0.01
    momentum = 0.5
    mu = None
    alpha = None
    beta = None
    decay_factor = None

    noise_level = 0

    """
    Perform runs
    """
    num_runs = 5

    noise_levels = [0]
    algorithms = ["fedavg", "fedprox", "ucb", "sfedavg", "poc"]
    # select_fractions = [10 / 700, 30 / 700, 50 / 700, 70 / 700, 90 / 700]
    select_fractions = [10 / 700, 90 / 700]
    # dirichlet_alphas = [1e-3, 1e-2, 1e-1, 1, 1e1, 1e2, 1e3]
    dirichlet_alphas = [1e-3, 1e3]

    sfedavg_alphas = [0, 0.25, 0.5, 0.75]
    poc_decay_factors = [1, 0.9]
    fedprox_mus = [0.001, 0.01, 0.1, 1]
    ucb_betas = [0.01, 0.1, 1, 10]

    for select_fraction in select_fractions:
        for dataset_alpha in dirichlet_alphas:
            for algorithm in algorithms:
                if algorithm == "sfedavg":
                    for alpha in sfedavg_alphas:
                        beta = 1 - alpha

                        test_run = AlgoRun(
                            dataset_config,
                            algorithm,
                            select_fraction,
                            E=E,
                            B=B,
                            T=T,
                            lr=lr,
                            momentum=momentum,
                            mu=mu,
                            alpha=alpha,
                            beta=beta,
                            decay_factor=decay_factor,
                            noise_level=noise_level,
                        )
                        avg_runs(num_runs, test_run, logging=True)

                elif algorithm == "fedavg":
                    test_run = AlgoRun(
                        dataset_config,
                        algorithm,
                        select_fraction,
                        E=E,
                        B=B,
                        T=T,
                        lr=lr,
                        momentum=momentum,
                        mu=mu,
                        alpha=alpha,
                        beta=beta,
                        decay_factor=decay_factor,
                        noise_level=noise_level,
                    )
                    avg_runs(num_runs, test_run, logging=True)
                elif algorithm == "poc":
                    for decay_factor in poc_decay_factors:
                        test_run = AlgoRun(
                            dataset_config,
                            algorithm,
                            select_fraction,
                            E=E,
                            B=B,
                            T=T,
                            lr=lr,
                            momentum=momentum,
                            mu=mu,
                            alpha=alpha,
                            beta=beta,
                            decay_factor=decay_factor,
                            noise_level=noise_level,
                        )
                        avg_runs(num_runs, test_run, logging=True)
                elif algorithm == "fedprox":
                    for mu in fedprox_mus:
                        test_run = AlgoRun(
                            dataset_config,
                            algorithm,
                            select_fraction,
                            E=E,
                            B=B,
                            T=T,
                            lr=lr,
                            momentum=momentum,
                            mu=mu,
                            alpha=alpha,
                            beta=beta,
                            decay_factor=decay_factor,
                            noise_level=noise_level,
                        )
                        avg_runs(num_runs, test_run, logging=True)
                elif algorithm == "ucb":
                    for beta in ucb_betas:
                        test_run = AlgoRun(
                            dataset_config,
                            algorithm,
                            select_fraction,
                            E=E,
                            B=B,
                            T=T,
                            lr=lr,
                            momentum=momentum,
                            mu=mu,
                            alpha=alpha,
                            beta=beta,
                            decay_factor=decay_factor,
                            noise_level=noise_level,
                        )
                        avg_runs(num_runs, test_run, logging=True)

    wandb.init(project="FL-AAU-11-7", name="finishing-2")
    wandb.alert(title="finished run 1", text="Finishing synthetic(1,1) run")
    wandb.finish()
