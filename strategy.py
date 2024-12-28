from imports import *
from models import SparseAutoencoder, MobileNetV3
from flower_client import get_parameters
from utils import aggregated_parameters_to_state_dict
import re
import os
from datetime import datetime
import psutil
import GPUtil
import h5py
from sklearn.cluster import KMeans
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import MinMaxScaler
import numpy as np

class FedCustom(Strategy):
    def __init__(
        self,
        fraction_fit: float = 1.0,
        fraction_evaluate: float = 1.0,
        min_fit_clients: int = 2,
        min_evaluate_clients: int = 2,
        min_available_clients: int = 2,
        initial_lr: float = 0.0005,
        step_size: int = 30,
        gamma: float = 0.9,
        model_type: str = "Image Classification",
        num_clusters: int = 3,
    ) -> None:
        with open('Default.txt', 'r') as f:
            config = dict(line.strip().split('=') for line in f if '=' in line)

        dynamic_grouping = float(config.get('dynamic_grouping', 0))
        clustering_frequency = int(config.get('clustering_frequency', 1))  # Fetch the correct frequency value

        self.dynamic_grouping = dynamic_grouping
        self.clustering_frequency = clustering_frequency
        self.fraction_fit = fraction_fit
        self.fraction_evaluate = fraction_evaluate
        self.min_fit_clients = min_fit_clients
        self.min_evaluate_clients = min_evaluate_clients
        self.min_available_clients = min_available_clients
        self.redistributed_parameters = {}
        self.initial_lr = initial_lr
        self.step_size = step_size
        self.gamma = gamma
        self.scheduler = None
        self.model_type = model_type
        self.num_clusters = num_clusters  # Fixed number of clusters
        self.cluster_labels = None
        self.cluster_models = {cluster: None for cluster in range(self.num_clusters)}

        # Create a new subfolder within "results" using model type, date, and time
        self.results_subfolder = os.path.join(
            "results", f"{self.model_type}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
        )
        os.makedirs(self.results_subfolder, exist_ok=True)

        # Initialize the resource consumption log file
        self.resource_consumption_file = os.path.join(self.results_subfolder, "resource_consumption.txt")
        self.initialize_resource_log()


    def initialize_resource_log(self):
        """Initialize the resource consumption log file with column headers."""
        cpu_count = psutil.cpu_count(logical=True)
        total_memory = round(psutil.virtual_memory().total / (1024 ** 3), 3)  # in GB
        gpus = GPUtil.getGPUs()
        gpu_name = gpus[0].name if gpus else "N/A"
        total_gpu_memory = round(gpus[0].memoryTotal, 3) if gpus else "N/A"  # in MB

        with open(self.resource_consumption_file, 'w') as file:
            file.write(f"Resource Consumption Log\n")
            file.write(f"CPU (Cores: {cpu_count}), GPU (Model: {gpu_name}, Memory: {total_gpu_memory} MB), Memory (Total: {total_memory} GB), Network (Bytes Sent/Received)\n")
            file.write("Round, Aggregated CPU Usage (%), Aggregated GPU Usage (%), Avg Memory Usage (%), Avg Network Sent (MB), Avg Network Received (MB)\n")

    def log_resource_consumption(self, server_round, client_metrics):
        """Aggregate and log client resource consumption for the round."""
        total_cpu = sum(metric["cpu"] for metric in client_metrics)
        total_gpu = sum(metric["gpu"] for metric in client_metrics)
        avg_memory = round(sum(metric["memory"] for metric in client_metrics) / len(client_metrics), 3)
        avg_net_sent = round(sum(metric["net_sent"] for metric in client_metrics) / len(client_metrics), 3)
        avg_net_received = round(sum(metric["net_received"] for metric in client_metrics) / len(client_metrics), 3)

        with open(self.resource_consumption_file, 'a') as file:
            file.write(f"{server_round}, {round(total_cpu, 3)}, {round(total_gpu, 3)}, {avg_memory}, {avg_net_sent}, {avg_net_received}\n")

    def initialize_parameters(self, client_manager: ClientManager) -> Optional[Parameters]:
        """Initialize global model parameters based on the model type."""
        if self.model_type == "Image Anomaly Detection":
            net = SparseAutoencoder()
        elif self.model_type == "Image Classification":
            net = MobileNetV3()
        else:
            raise ValueError(f"Unsupported model_type: {self.model_type}")

        ndarrays = get_parameters(net)
        return fl.common.ndarrays_to_parameters(ndarrays)

    def configure_fit(self, server_round: int, parameters: Parameters, client_manager: ClientManager) -> List[Tuple[fl.server.client_proxy.ClientProxy, FitIns]]:
        """Configure the next round of training with optional dynamic grouping."""
        num_clients = len(client_manager)
        if num_clients < self.min_fit_clients:
            return []

        sample_size = int(num_clients * self.fraction_fit)
        sample_size = max(sample_size, self.min_fit_clients)
        clients = client_manager.sample(num_clients=sample_size, min_num_clients=self.min_fit_clients)

        fit_configurations = []
        for client in clients:
            client_id = int(client.cid)

            # Apply dynamic grouping logic only if enabled
            if self.dynamic_grouping == 1 and server_round > 1:
                # Ensure client_cluster_mapping is initialized and client_id exists in the mapping
                if hasattr(self, 'client_cluster_mapping') and client_id in self.client_cluster_mapping:
                    cluster = self.client_cluster_mapping[client_id]
                    cluster_parameters = self.cluster_models[cluster]
                else:
                    # If client_id is not in the mapping, use default parameters
                    cluster_parameters = parameters
            else:
                cluster_parameters = parameters

            fit_configurations.append((client, FitIns(cluster_parameters, {"server_round": server_round})))

        return fit_configurations

    def aggregate_parameters(self, parameters_list: List[List[np.ndarray]], server_round: int) -> Tuple[List[np.ndarray], Optional[np.ndarray]]:
        """Aggregate model parameters with optional dynamic grouping and dynamic thresholding."""
        num_models = len(parameters_list)
        cluster_labels = None

        if self.dynamic_grouping == 1:
            # Apply clustering only on the first round or at intervals of clustering_frequency
            if server_round == 1 or server_round % self.clustering_frequency == 0:
                # Flatten the parameter arrays to create a feature vector for each model
                flattened_parameters = [np.concatenate([param.flatten() for param in params]) for params in parameters_list]

                # Perform clustering using cosine similarity
                similarity_matrix = cosine_similarity(flattened_parameters)

                # Dynamically assign clusters
                cluster_labels = self.assign_clusters_with_dynamic_threshold(similarity_matrix, num_clusters=self.num_clusters)
                self.cluster_labels = cluster_labels  # Save the new cluster labels for use in subsequent rounds

                # Save the mapping of client IDs to cluster labels for consistency
                self.client_cluster_mapping = {i: cluster_labels[i] for i in range(num_models)}
            else:
                # Use previously stored cluster labels if not clustering round
                cluster_labels = self.cluster_labels
                if cluster_labels is None:
                    raise ValueError("Cluster labels not initialized.")

                # Ensure client_cluster_mapping is initialized correctly
                if not hasattr(self, 'client_cluster_mapping') or len(self.client_cluster_mapping) < num_models:
                    self.client_cluster_mapping = {i: cluster_labels[i] for i in range(num_models)}

                # Use the saved client-cluster mapping to ensure consistency
                cluster_labels = np.array([self.client_cluster_mapping.get(i, cluster_labels[i]) for i in range(num_models)])

            # Aggregate parameters within each cluster
            aggregated_parameters = []
            for cluster in range(self.num_clusters):
                cluster_parameters = [parameters_list[i] for i in range(num_models) if cluster_labels[i] == cluster]
                if cluster_parameters:
                    cluster_aggregated_parameters = [np.mean(np.array(param_tuple), axis=0) for param_tuple in zip(*cluster_parameters)]
                    aggregated_parameters.append(cluster_aggregated_parameters)

            # Further aggregate the cluster centers to obtain the final parameters
            if aggregated_parameters:
                final_aggregated_parameters = [np.mean(np.array(param_tuple), axis=0) for param_tuple in zip(*aggregated_parameters)]
            else:
                final_aggregated_parameters = [np.zeros_like(param) for param in parameters_list[0]]

            # Update the cluster models for the next round
            self.cluster_models = {cluster: fl.common.ndarrays_to_parameters(params) for cluster, params in enumerate(aggregated_parameters)}
        else:
            # Default global aggregation
            final_aggregated_parameters = [np.mean(param_tuple, axis=0) for param_tuple in zip(*parameters_list)]

        return final_aggregated_parameters, cluster_labels

    def assign_clusters_with_dynamic_threshold(self, similarity_matrix, num_clusters=3):
        """
        Dynamically assign clusters based on cosine similarity and a dynamic threshold
        while ensuring exactly `num_clusters` clusters.
        
        Args:
            similarity_matrix (np.ndarray): Pairwise cosine similarity matrix.
            num_clusters (int): Number of clusters to form (default: 3).
        
        Returns:
            np.ndarray: Array of cluster labels for each client.
        """
        num_clients = similarity_matrix.shape[0]
        
        # Calculate average similarity for each client
        avg_similarities = np.mean(similarity_matrix, axis=1)
        
        # Determine dynamic threshold (e.g., lower quartile for detecting outliers)
        min_similarity_threshold = np.percentile(avg_similarities, 25)  # Adjust sensitivity if needed
        
        # Identify low-similarity (potentially outlier) clients
        outlier_indices = np.where(avg_similarities < min_similarity_threshold)[0]
        normal_indices = np.where(avg_similarities >= min_similarity_threshold)[0]
        
        # If all clients are similar, use KMeans to split evenly
        if len(outlier_indices) == 0 or len(outlier_indices) == num_clients:
            kmeans = KMeans(n_clusters=num_clusters, random_state=0, n_init="auto")
            cluster_labels = kmeans.fit_predict(similarity_matrix)
            return cluster_labels

        # Assign clusters
        cluster_labels = np.zeros(num_clients, dtype=int)

        # Outliers get their own cluster (e.g., Cluster 0)
        cluster_labels[outlier_indices] = 0

        # Normal clients are split into remaining clusters using KMeans
        if len(normal_indices) > 0:
            normal_client_similarities = similarity_matrix[normal_indices][:, normal_indices]
            kmeans = KMeans(n_clusters=num_clusters - 1, random_state=0, n_init="auto")
            normal_clusters = kmeans.fit_predict(normal_client_similarities)
            # Map normal client clusters to the available cluster range (1 to num_clusters-1)
            for idx, cluster in zip(normal_indices, normal_clusters):
                cluster_labels[idx] = cluster + 1

        return cluster_labels


    def _save_cluster_assignments(self, results, cluster_labels, server_round):
        """Save the cluster assignments for each client in a single file with fixed client IDs assigned to clusters."""
        if self.dynamic_grouping != 1 or cluster_labels is None:
            return  # Skip saving if dynamic grouping is not enabled or cluster_labels is None.

        # Define the path to save cluster assignments
        cluster_assignment_file_path = os.path.join(self.results_subfolder, 'cluster_assignments.h5')
        cluster_assignment_txt_path = os.path.join(self.results_subfolder, 'cluster_assignments.txt')

        # Extract and sort client IDs numerically if possible
        client_ids = [client.cid for client, _ in results]
        try:
            client_ids = sorted(client_ids, key=lambda x: int(x))
        except ValueError:
            client_ids = sorted(client_ids)

        # Update the client-cluster mapping with fixed client assignments
        if server_round == 1 or server_round % self.clustering_frequency == 0:
            self.client_cluster_mapping = {client_id: cluster_labels[idx] for idx, client_id in enumerate(client_ids)}

        # Save the client-cluster mapping to the HDF5 file
        with h5py.File(cluster_assignment_file_path, 'a') as f:
            if str(server_round) not in f:
                grp = f.create_group(str(server_round))
                grp.create_dataset("client_ids", data=np.array(client_ids, dtype='S'))
                grp.create_dataset("cluster_labels", data=np.array([self.client_cluster_mapping[cid] for cid in client_ids], dtype='i'))
            else:
                grp = f[str(server_round)]
                grp["client_ids"][:] = np.array(client_ids, dtype='S')
                grp["cluster_labels"][:] = np.array([self.client_cluster_mapping[cid] for cid in client_ids], dtype='i')

        # Save to the TXT file with sorted entries
        with open(cluster_assignment_txt_path, 'a') as txt_file:
            txt_file.write(f"Server Round {server_round}:\n")
            for cid in client_ids:
                txt_file.write(f"Client ID: {cid}, Cluster: {self.client_cluster_mapping[cid]}\n")
            txt_file.write("\n")




    def aggregate_fit(
        self, server_round: int, results: List[Tuple[fl.server.client_proxy.ClientProxy, fl.common.FitRes]],
        failures: List[Union[Tuple[fl.server.client_proxy.ClientProxy, fl.common.FitRes], BaseException]]
    ) -> Tuple[Optional[fl.common.Parameters], Dict[str, fl.common.Scalar]]:
        """Aggregate fit results and save models for both dynamic and default grouping."""

        if not results:
            return None, {}

        parameters_list = [parameters_to_ndarrays(res.parameters) for client, res in results]

        if self.dynamic_grouping == 1:
            # Perform clustering and aggregate parameters for each cluster
            aggregated_parameters, cluster_labels = self.aggregate_parameters(parameters_list, server_round)
            self.cluster_labels = cluster_labels  # Store cluster labels for this round

            # Save cluster assignments
            self._save_cluster_assignments(results, cluster_labels, server_round)

        else:
            # Default global aggregation logic
            aggregated_parameters = [np.mean(param_tuple, axis=0) for param_tuple in zip(*parameters_list)]

        aggregated_parameters_fl = fl.common.ndarrays_to_parameters(aggregated_parameters)

        return aggregated_parameters_fl, {}


    def configure_evaluate(self, server_round: int, parameters: fl.common.Parameters, client_manager: fl.server.ClientManager) -> List[Tuple[fl.server.client_proxy.ClientProxy, fl.common.EvaluateIns]]:
        """Configure evaluation with optional dynamic grouping."""
        
        if self.fraction_evaluate == 0.0:
            return []

        config = {"server_round": server_round, "task": "evaluate"}

        sample_size, min_num_clients = self.num_evaluation_clients(client_manager.num_available())
        clients = client_manager.sample(num_clients=sample_size, min_num_clients=min_num_clients)

        evaluate_configurations = []
        for client in clients:
            client_id = int(client.cid)
            
            # Assign cluster-specific parameters to clients based on clustering
            if self.dynamic_grouping == 1 and server_round > 1:
                # Ensure client_cluster_mapping is initialized and client_id exists in the mapping
                if hasattr(self, 'client_cluster_mapping') and client_id in self.client_cluster_mapping:
                    cluster = self.client_cluster_mapping[client_id]
                    if cluster in self.cluster_models and self.cluster_models[cluster] is not None:
                        cluster_parameters = self.cluster_models[cluster]
                    else:
                        cluster_parameters = parameters  # Use global parameters if cluster model is not available
                else:
                    # If client_id is not in the mapping, use default parameters
                    cluster_parameters = parameters
            else:
                cluster_parameters = parameters

            evaluate_ins = fl.common.EvaluateIns(cluster_parameters, config=config)
            evaluate_configurations.append((client, evaluate_ins))

        return evaluate_configurations


    def log_all_clients_hardware_resources(self, server_round, client_results):
        """Log each client's hardware usage in hardware_resources.ncol and aggregate CPU/GPU for resource_consumption.txt."""
        hardware_file_path = os.path.join(self.results_subfolder, 'hardware_resources.ncol')
        client_metrics = []

        with open(hardware_file_path, 'a') as file:
            file.write(f"Round {server_round}\n")
            for client, res in client_results:
                cpu_usage = round(psutil.cpu_percent(interval=1), 3)
                gpus = GPUtil.getGPUs()
                gpu_usage = round(gpus[0].load * 100, 3) if gpus else 0
                memory_usage = round(psutil.virtual_memory().percent, 3)
                net_io = psutil.net_io_counters()
                net_sent = round(net_io.bytes_sent / (1024 ** 2), 3)
                net_received = round(net_io.bytes_recv / (1024 ** 2), 3)

                client_metrics.append({
                    "cpu": cpu_usage,
                    "gpu": gpu_usage,
                    "memory": memory_usage,
                    "net_sent": net_sent,
                    "net_received": net_received
                })

                file.write(f"Client {client.cid}: CPU {cpu_usage}%, GPU {gpu_usage}%, Memory {memory_usage}%, Network Sent: {net_sent}MB, Network Received: {net_received}MB\n")

        # After logging each client's data, log the aggregated metrics
        self.log_resource_consumption(server_round, client_metrics)

    def _compute_group_metrics(self, results):
        """Compute average metrics for each group in dynamic grouping."""
        group_metrics = [{'accuracy': 0.0, 'f1_score': 0.0, 'log_loss': 0.0} for _ in range(self.num_clusters)]
        group_counts = [0] * self.num_clusters

        for client, res in results:
            cluster_idx = self.cluster_labels[int(client.cid) % len(self.cluster_labels)]
            metrics = res.metrics
            accuracy = metrics.get('accuracy', 0)
            f1 = metrics.get('f1_score', 0)
            log_loss_value = metrics.get('log_loss', 0)
            num_examples = res.num_examples

            group_metrics[cluster_idx]['accuracy'] += accuracy * num_examples
            group_metrics[cluster_idx]['f1_score'] += f1 * num_examples
            group_metrics[cluster_idx]['log_loss'] += log_loss_value * num_examples
            group_counts[cluster_idx] += num_examples

        # Compute averages
        for idx in range(self.num_clusters):
            if group_counts[idx] > 0:
                group_metrics[idx]['accuracy'] /= group_counts[idx]
                group_metrics[idx]['f1_score'] /= group_counts[idx]
                group_metrics[idx]['log_loss'] /= group_counts[idx]

        return group_metrics


    def _select_best_model(self, server_round: int, evaluation_file_path: str) -> Tuple[int, float]:
        """Identify the best-performing cluster based on a specified evaluation metric."""
        best_cluster = None
        best_performance = -float('inf')  # Assuming higher metric is better (e.g., accuracy)
        metric_to_use = 'Accuracy'  # Change this to 'Accuracy', 'F1 Score', or 'Log Loss' as needed

        # Read the evaluation file and find the metrics for the current round
        with open(evaluation_file_path, 'r') as file:
            lines = file.readlines()

        # Locate the round's metrics in the file
        round_found = False
        for line in lines:
            if f"Round {server_round}" in line:
                round_found = True
                continue
            if round_found and line.strip().startswith("Group-"):
                # Extract group number
                match_group = re.match(r'Group-(\d+):', line)
                if match_group:
                    group = int(match_group.group(1))
                else:
                    continue  # Skip if no match

                # Extract the desired metric
                pattern = rf'{metric_to_use}:\s*([\d\.]+)'
                match_metric = re.search(pattern, line)
                if match_metric:
                    metric_value = float(match_metric.group(1))
                else:
                    continue  # Skip if the metric is not found

                # Update best cluster based on the specified metric
                if metric_value > best_performance:
                    best_performance = metric_value
                    best_cluster = group

            elif round_found and line.strip() == "":
                # End of the current round's section
                break

        if best_cluster is None:
            raise ValueError(f"No metrics found for Round {server_round} in {evaluation_file_path}")

        return best_cluster - 1, best_performance  # Adjust cluster index to match 0-based indexing


    def _select_best_model_and_save(self, server_round: int):
        """Select the best-performing model based on evaluation loss and save it as the global model."""
        evaluation_file_path = os.path.join(self.results_subfolder, "evaluation_loss.txt")
        best_cluster, best_performance = self._select_best_model(server_round, evaluation_file_path)

        print(f"Best model selected from Cluster-{best_cluster} with performance: {best_performance:.4f}")

        # Use the best cluster model and save it as the global model
        best_model_parameters = self.cluster_models[best_cluster]
        if self.model_type == "Image Anomaly Detection":
            global_net = SparseAutoencoder()
        elif self.model_type == "Image Classification":
            global_net = MobileNetV3()

        state_dict = aggregated_parameters_to_state_dict(parameters_to_ndarrays(best_model_parameters), self.model_type)
        global_net.load_state_dict(state_dict)

        # Save the best-performing model with a specific name
        best_model_path = os.path.join(self.results_subfolder, "best_cluster_model.pth")
        torch.save(global_net.state_dict(), best_model_path)
        print(f"Best cluster model saved as {best_model_path}.")



    def aggregate_evaluate(
        self, server_round: int, results: List[Tuple[fl.server.client_proxy.ClientProxy, fl.common.EvaluateRes]],
        failures: List[Union[Tuple[fl.server.client_proxy.ClientProxy, fl.common.EvaluateRes], BaseException]]
    ) -> Tuple[Optional[float], Dict[str, fl.common.Scalar]]:
        """Aggregate evaluation results, save metrics, and select the best-performing model."""
        
        if not results:
            return None, {}

        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # Paths for metric files
        accuracy_file_path = os.path.join(self.results_subfolder, 'accuracy_scores.ncol')
        f1_score_file_path = os.path.join(self.results_subfolder, 'F1_scores.ncol')
        logloss_file_path = os.path.join(self.results_subfolder, 'LogLoss_scores.ncol')
        
        # Determine output file based on dynamic grouping
        evaluation_file_name = 'evaluation_loss.txt' if self.dynamic_grouping == 1 else 'aggregated_evaluation_loss.txt'
        evaluation_file_path = os.path.join(self.results_subfolder, evaluation_file_name)

        total_accuracy = 0.0
        total_f1 = 0.0
        total_logloss = 0.0
        total_examples = 0
        accuracy_scores = []
        f1_scores = []
        logloss_scores = []
        self.log_all_clients_hardware_resources(server_round, results)

        # Aggregate client metrics
        for client, res in results:
            if self.model_type == "Image Classification":
                # Get metrics
                accuracy = res.metrics.get('accuracy', 0)
                f1 = res.metrics.get('f1_score', 0)
                logloss = res.metrics.get('log_loss', 0)

                num_examples = res.num_examples

                # Append to lists
                accuracy_scores.append((client.cid, accuracy))
                f1_scores.append((client.cid, f1))
                logloss_scores.append((client.cid, logloss))

                total_accuracy += accuracy * num_examples
                total_f1 += f1 * num_examples
                total_logloss += logloss * num_examples
                total_examples += num_examples

            elif self.model_type == "Image Anomaly Detection":
                # Existing code for anomaly detection (unchanged)
                pass

        # Calculate aggregated metrics
        aggregated_accuracy = total_accuracy / total_examples if total_examples > 0 else None
        aggregated_f1 = total_f1 / total_examples if total_examples > 0 else None
        aggregated_logloss = total_logloss / total_examples if total_examples > 0 else None

        # Save client scores
        # Sort by client ID
        accuracy_scores.sort(key=lambda x: int(x[0]))
        f1_scores.sort(key=lambda x: int(x[0]))
        logloss_scores.sort(key=lambda x: int(x[0]))

        # Save accuracy scores
        with open(accuracy_file_path, 'a') as file:
            file.write(f"Time: {current_time} - Round {server_round}\n")
            for cid, metric_value in accuracy_scores:
                file.write(f"{cid} {metric_value}\n")

        # Save F1 scores
        with open(f1_score_file_path, 'a') as file:
            file.write(f"Time: {current_time} - Round {server_round}\n")
            for cid, f1_value in f1_scores:
                file.write(f"{cid} {f1_value}\n")

        # Save Log Loss scores
        with open(logloss_file_path, 'a') as file:
            file.write(f"Time: {current_time} - Round {server_round}\n")
            for cid, logloss_value in logloss_scores:
                file.write(f"{cid} {logloss_value}\n")

        # Save grouped or aggregated metrics
        with open(evaluation_file_path, 'a') as file:
            file.write(f"Time: {current_time} - Round {server_round}\n")
            if self.dynamic_grouping == 1 and self.cluster_labels is not None:
                group_metrics = self._compute_group_metrics(results)
                for group_idx, metrics in enumerate(group_metrics, start=1):
                    file.write(
                        f"Group-{group_idx}: Accuracy: {metrics['accuracy']:.4f}, "
                        f"F1 Score: {metrics['f1_score']:.4f}, Log Loss: {metrics['log_loss']:.4f}\n"
                    )
            else:
                file.write(
                    f"Aggregated Metrics: Accuracy: {aggregated_accuracy:.4f}, "
                    f"F1 Score: {aggregated_f1:.4f}, Log Loss: {aggregated_logloss:.4f}\n"
                )


        # Save the best-performing model as the global model after evaluation
        if self.dynamic_grouping == 1:
            self._select_best_model_and_save(server_round)

        return aggregated_accuracy, {}


    def evaluate(
        self, server_round: int, parameters: fl.common.Parameters
    ) -> Optional[Tuple[float, Dict[str, fl.common.Scalar]]]:
        """Evaluate global model parameters using an evaluation function."""
        return None

    def num_fit_clients(self, num_available_clients: int) -> Tuple[int, int]:
        """Return sample size and required number of clients for fitting."""
        num_clients = int(num_available_clients * self.fraction_fit)
        return max(num_clients, self.min_fit_clients), self.min_available_clients

    def num_evaluation_clients(self, num_available_clients: int) -> Tuple[int, int]:
        """Return sample size and required number of clients for evaluation."""
        num_clients = int(num_available_clients * self.fraction_evaluate)
        return max(num_clients, self.min_evaluate_clients), self.min_available_clients

    def detect_potential_poisoned_client(self, server_round: int, local_updates):
        # Load the best cluster model (only for classification)
        best_model_path = os.path.join(self.results_subfolder, "best_cluster_model.pth")
        if self.model_type != "Image Classification":
            raise ValueError("Poison detection is only supported for Image Classification.")

        # Initialize the model and load the best model's state
        best_model = MobileNetV3()
        best_model.load_state_dict(torch.load(best_model_path))
        best_last_layer = next(reversed(best_model.state_dict().values()))  # Extract the last layer of the best cluster model

        # Extract local updates and compute cosine similarity
        client_scores = {}
        for update in local_updates:
            client_id = update["client_id"]
            local_parameters = parameters_to_ndarrays(update["model"])
            local_model_state = aggregated_parameters_to_state_dict(local_parameters, self.model_type)

            # Compute cosine similarity
            local_last_layer = next(reversed(local_model_state.values()))
            similarity = cosine_similarity(best_last_layer.reshape(1, -1), local_last_layer.reshape(1, -1))[0][0]
            client_scores[client_id] = similarity

        # Identify the client with the lowest similarity score
        potential_poisoned_client = min(client_scores, key=client_scores.get)

        # Save detection results
        poisoned_log_path = os.path.join(self.results_subfolder, "poisoned_client_detection.txt")
        with open(poisoned_log_path, 'a') as log_file:
            log_file.write(f"Round {server_round} - Potential Poisoned Client Detection\n")
            log_file.write(f"Potential Poisoned Client: Client-{potential_poisoned_client}\n")
            log_file.write(f"Similarity Scores: {client_scores}\n")

        print(f"Detection results saved for round {server_round} in {poisoned_log_path}.")

def aggregated_parameters_to_state_dict(aggregated_parameters, model_type):
    """Convert aggregated parameters to a state dictionary based on model type."""
    if model_type == "Image Anomaly Detection":
        param_keys = list(SparseAutoencoder().state_dict().keys())
    elif model_type == "Image Classification":
        param_keys = list(MobileNetV3().state_dict().keys())
    else:
        raise ValueError(f"Unsupported model_type: {model_type}")

    state_dict = {key: torch.tensor(param) for key, param in zip(param_keys, aggregated_parameters)}
    return state_dict