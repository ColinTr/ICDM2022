from sklearn.metrics import accuracy_score, balanced_accuracy_score, adjusted_rand_score, normalized_mutual_info_score
from scipy.optimize import linear_sum_assignment as linear_assignment
import matplotlib.pyplot as plt
import torch.nn.functional as F
import numpy as np
import logging
import torch
import math
import os

from src.loss_functions import vime_loss


def setup_device(use_cuda=True):
    """
    Initialize the torch device where the code will be executed on.

    :param use_cuda: Set to True if you want the code to be run on your GPU. If set to False, code will run on CPU.
    :return: torch.device : The initialized device, torch.device.
    """
    if use_cuda is False or not torch.cuda.is_available():
        device_name = "cpu"
        if use_cuda is True:
            logging.critical("unable to initialize CUDA, check torch installation (https://pytorch.org/)")
        if use_cuda is False:
            os.environ["CUDA_VISIBLE_DEVICES"] = ""
    else:
        device_name = "cuda:0"
        logging.info("CUDA successfully initialized on device : " + torch.cuda.get_device_name())

    device = torch.device(device_name)

    logging.info("Using device : " + device.type)

    return device


def setup_logging_level(level):
    """
    Sets up the logging level.
    :param level: String, the logging level.
    """
    if level == 'debug':
        logging.getLogger().setLevel(logging.DEBUG)
    elif level == 'info':
        logging.getLogger().setLevel(logging.INFO)
    elif level == 'warning':
        logging.getLogger().setLevel(logging.WARNING)
    else:
        raise ValueError('Unknown parameter for log_lvl.')


def pretext_generator(m, x):
    """
    Generation of corrupted samples.
    This is a sped up version of the original pretext_generator of VIME's code.
    It is about 5 times faster and should be equivalent.

    :param m: The corruption mask, np.array with shape (n_samples, n_features).
    :param x: The set to corrupt, np.array with shape (n_samples, n_features).
    :return:
        m_new: The new corruption mask, np.array with shape (n_samples, n_features).
        x_tilde: The corrupted samples, np.array with shape (n_samples, n_features).
    """
    # Randomly (and column-wise) shuffle data
    x_bar = x.copy()
    np.random.shuffle(x_bar)

    # Corrupt samples
    x_tilde = x * (1 - m) + x_bar * m

    # Define new mask matrix (as it is possible that the corrupted samples are the same as the original ones)
    m_new = 1 * (x != x_tilde)

    return m_new, x_tilde


def evaluate_vime_model_on_set(x_input, model, device, batch_size=100, p_m=0.3):
    """
    Method that evaluates the feature and mask estimation of corrupted samples of the given model on x_input.

    :param x_input: The input dataset, torch.Tensor of shape (n_samples, n_features).
    :param model: The model to evaluate, torch.nn.Module.
    :param device: The device to send the dataset to, torch.device.
    :param p_m: The corruption probability, int.
    :param batch_size: The batch size (default=100, reduce if GPU is low on memory), int.
    :return:
        mean_mask_loss: The mean mask estimation loss, float.
        mean_feature_loss: The mean feature estimation loss, float.
    """
    mask_losses, feature_losses = [], []
    test_batch_start_index, test_batch_end_index = 0, batch_size
    for batch_index in range(math.ceil((x_input.shape[0]) / batch_size)):
        batch_x_input = x_input[test_batch_start_index:test_batch_end_index]

        m_unlab = np.random.binomial(1, p_m, batch_x_input.shape)
        m_label, x_tilde = pretext_generator(m_unlab, batch_x_input.to('cpu').numpy())
        x_tilde = torch.Tensor(x_tilde).to(device)
        m_label = torch.Tensor(m_label).to(device)

        model.eval()
        with torch.no_grad():
            mask_pred, feature_pred = model.vime_forward(x_tilde)
        model.train()

        mask_loss, feature_loss = vime_loss(mask_pred, m_label, feature_pred, batch_x_input)
        mask_losses.append(mask_loss.item())
        feature_losses.append(feature_loss.item())

        test_batch_start_index += batch_size
        test_batch_end_index = test_batch_end_index + batch_size if test_batch_end_index + batch_size < x_input.shape[0] else x_input.shape[0]

    return np.mean(mask_losses), np.mean(feature_losses)


def pretty_mean(my_list):
    """
    Simple method to avoid displaying error messages when the
    :param my_list:
    :return:
    """
    return np.mean(my_list) if len(my_list) > 0 else -1


def compute_classification_accuracy(x_test, y_test, y_train, model):
    """
    ToDo : Documentation
    """
    # Define a mapping of the train classes, as they may not range from 0 to C
    mapper, ind = np.unique(y_train, return_inverse=True)

    x_test_known_mask = np.in1d(y_test, np.unique(y_train))
    x_test_known = x_test[x_test_known_mask]
    y_test_known = y_test[x_test_known_mask]

    model.eval()
    with torch.no_grad():
        # Forward the classification head only for known classes
        x_test_known_projection = model.encoder_forward(x_test_known)
        model_y_test_known_pred = model.classification_head_forward(x_test_known_projection)
    model.train()

    model_y_test_known_pred = F.softmax(model_y_test_known_pred, -1)  # Apply softmax
    model_y_test_known_pred = torch.argmax(model_y_test_known_pred, dim=1)  # Get the prediction from the probabilities
    model_y_test_known_pred = mapper[model_y_test_known_pred.cpu().numpy()]  # Map the prediction back to the true labels

    return accuracy_score(y_test_known, model_y_test_known_pred)


def compute_clustering_accuracy(x_test, y_test, y_unlab, model):
    """
    Compute the clustering accuracy.
    The computation is based on the assignment of the most probable clusters using scipy's linear_sum_assignment.

    :param x_test: ToDo : Documentation
    :param y_test: ToDo : Documentation
    :param y_unlab: ToDo : Documentation
    :param model: ToDo : Documentation
    :return: Accuracy between 0 and 1.
    """
    # (1) Get the prediction of the model
    x_test_unknown_mask = np.in1d(y_test, np.unique(y_unlab))
    x_test_unknown = x_test[x_test_unknown_mask]
    y_test_unknown = y_test[x_test_unknown_mask]

    model.eval()
    with torch.no_grad():
        x_test_unknown_projection = model.encoder_forward(x_test_unknown)
        model_y_test_unknown_pred = model.clustering_head_forward(x_test_unknown_projection)
    model.train()

    model_y_test_unknown_pred = F.softmax(model_y_test_unknown_pred, -1)
    model_y_test_unknown_pred = torch.argmax(model_y_test_unknown_pred, dim=1)
    model_y_test_unknown_pred = model_y_test_unknown_pred.cpu().numpy()

    # (2) Compute the clustering accuracy using the hungarian algorithm
    y_test_unknown = y_test_unknown.astype(np.int64)
    assert model_y_test_unknown_pred.size == y_test_unknown.size
    D = max(model_y_test_unknown_pred.max(), y_test_unknown.max()) + 1
    w = np.zeros((D, D), dtype=np.int64)
    for i in range(model_y_test_unknown_pred.size):
        w[model_y_test_unknown_pred[i], y_test_unknown[i]] += 1
    ind = linear_assignment(w.max() - w)  # The hungarian algorithm

    acc = sum([w[i, j] for i, j in zip(ind[0], ind[1])]) * 1.0 / model_y_test_unknown_pred.size

    return acc


def compute_balanced_clustering_accuracy(x_test, y_test, y_unlab, model):
    """
    Compute the clustering accuracy.
    The computation is based on the assignment of the most probable clusters using scipy's linear_sum_assignment.

    :param x_test: ToDo : Documentation
    :param y_test: ToDo : Documentation
    :param y_unlab: ToDo : Documentation
    :param model: ToDo : Documentation
    :return: Accuracy between 0 and 1.
    """
    # (1) Get the prediction of the model
    x_test_unknown_mask = np.in1d(y_test, np.unique(y_unlab))
    x_test_unknown = x_test[x_test_unknown_mask]
    y_test_unknown = y_test[x_test_unknown_mask]

    model.eval()
    with torch.no_grad():
        x_test_unknown_projection = model.encoder_forward(x_test_unknown)
        model_y_test_unknown_pred = model.clustering_head_forward(x_test_unknown_projection)
    model.train()

    model_y_test_unknown_pred = F.softmax(model_y_test_unknown_pred, -1)
    model_y_test_unknown_pred = torch.argmax(model_y_test_unknown_pred, dim=1)
    model_y_test_unknown_pred = model_y_test_unknown_pred.cpu().numpy()

    # (2) Compute the clustering accuracy using the hungarian algorithm
    y_test_unknown = y_test_unknown.astype(np.int64)
    assert model_y_test_unknown_pred.size == y_test_unknown.size
    D = max(model_y_test_unknown_pred.max(), y_test_unknown.max()) + 1
    w = np.zeros((D, D), dtype=np.int64)
    for i in range(model_y_test_unknown_pred.size):
        w[model_y_test_unknown_pred[i], y_test_unknown[i]] += 1
    ind = linear_assignment(w.max() - w)  # The hungarian algorithm

    # Balanced accuracy
    permutations_dict = dict(zip(ind[0], ind[1]))
    return balanced_accuracy_score(y_test_unknown, list(map(permutations_dict.get, model_y_test_unknown_pred)))


def compute_ari_and_nmi(x_test, y_test, y_unlab, model):
    """
    ToDo Documentation
    """
    x_test_known_mask = np.in1d(y_test, np.unique(y_unlab))
    x_test_known = x_test[x_test_known_mask]
    y_test_known = y_test[x_test_known_mask]

    model.eval()
    with torch.no_grad():
        model_y_test_known_pred = model.clustering_head_forward(model.encoder_forward(x_test_known))
    model.train()
    model_y_test_known_pred = F.softmax(model_y_test_known_pred, -1)
    model_y_test_known_pred = torch.argmax(model_y_test_known_pred, dim=1)

    # Both metrics are independent of the absolute values of the labels
    # So a permutation of the class or cluster label values won’t change the score value in any way.
    ari = adjusted_rand_score(y_test_known, model_y_test_known_pred.cpu().numpy())
    nmi = normalized_mutual_info_score(y_test_known, model_y_test_known_pred.cpu().numpy())
    return ari, nmi


def PairEnum(x):
    # Enumerate all pairs of feature in x
    assert x.ndimension() == 2, 'Input dimension must be 2'
    x1 = x.repeat(x.size(0), 1)
    x2 = x.repeat(1, x.size(0)).view(-1, x.size(1))
    return x1, x2


def ranking_stats_pseudo_labels(encoded_x_unlab, device, topk=5):
    rank_idx = torch.argsort(encoded_x_unlab, dim=1, descending=True)
    rank_idx1, rank_idx2 = PairEnum(rank_idx)
    rank_idx1, rank_idx2 = rank_idx1[:, :topk], rank_idx2[:, :topk]
    rank_idx1, _ = torch.sort(rank_idx1, dim=1)
    rank_idx2, _ = torch.sort(rank_idx2, dim=1)
    rank_diff = rank_idx1 - rank_idx2
    rank_diff = torch.sum(torch.abs(rank_diff), dim=1)
    target_ulb = torch.ones_like(rank_diff).float().to(device)
    target_ulb[rank_diff > 0] = 0
    return target_ulb


def plot_alternative_joint_learning_metrics(metrics_dict, figure_path):
    f, axes = plt.subplots(3, 3, figsize=(12, 12))

    axes[0, 0].plot(range(1, len(metrics_dict['balanced_train_clustering_accuracy']) + 1), metrics_dict['balanced_train_clustering_accuracy'], '-o', label='Train acc.', color='blue')
    axes[0, 0].plot(range(1, len(metrics_dict['balanced_test_clustering_accuracy']) + 1), metrics_dict['balanced_test_clustering_accuracy'], '-o', label='Test acc.', color='orange')
    axes[0, 0].set_title('Train/Test balanced clustering accuracy')
    axes[0, 0].set_ylabel('Balanced clustering accuracy')
    axes[0, 0].legend()

    axes[0, 1].plot(range(1, len(metrics_dict['train_clustering_accuracy']) + 1), metrics_dict['train_clustering_accuracy'], '-o', label='Train acc.', color='blue')
    axes[0, 1].plot(range(1, len(metrics_dict['test_clustering_accuracy']) + 1), metrics_dict['test_clustering_accuracy'], '-o', label='Test acc.', color='orange')
    axes[0, 1].set_title('Train/Test clustering accuracy')
    axes[0, 1].set_ylabel('Clustering accuracy')
    axes[0, 1].legend()

    axes[0, 2].plot(range(1, len(metrics_dict['train_classification_accuracy']) + 1), metrics_dict['train_classification_accuracy'], '-o', label='Train acc.', color='blue')
    axes[0, 2].plot(range(1, len(metrics_dict['test_classification_accuracy']) + 1), metrics_dict['test_classification_accuracy'], '-o', label='Test acc.', color='orange')
    axes[0, 2].set_title('Train/Test classification accuracy')
    axes[0, 2].set_ylabel('Classification accuracy')
    axes[0, 2].legend()

    axes[1, 0].plot(range(1, len(metrics_dict['train_classification_losses']) + 1), metrics_dict['train_classification_losses'], '-o', label='Loss')
    axes[1, 0].set_title('Classification loss')

    axes[1, 1].plot(range(1, len(metrics_dict['ce_losses']) + 1), metrics_dict['ce_losses'], '-o', label='Loss')
    axes[1, 1].set_title('Cross entropy loss')

    axes[1, 2].plot(range(1, len(metrics_dict['train_cs_classification_losses']) + 1), metrics_dict['train_cs_classification_losses'], '-o', label='Loss')
    axes[1, 2].set_title('Classifier consistency loss')

    axes[2, 0].plot(range(1, len(metrics_dict['train_clustering_losses']) + 1), metrics_dict['train_clustering_losses'], '-o', label='Loss')
    axes[2, 0].set_title('Clustering loss')

    axes[2, 1].plot(range(1, len(metrics_dict['bce_losses']) + 1), metrics_dict['bce_losses'], '-o', label='Loss')
    axes[2, 1].set_title('Binary cross entropy loss')

    axes[2, 2].plot(range(1, len(metrics_dict['train_cs_clustering_losses']) + 1), metrics_dict['train_cs_clustering_losses'], '-o', label='Loss')
    axes[2, 2].set_title('Clustering consistency loss')

    [[axes[i, j].set_xlabel("Epoch") for i in range(3)] for j in range(3)]
    [[axes[i, j].set_ylabel("Loss") for i in range(1, 3)] for j in range(3)]

    plt.tight_layout()

    plt.savefig(figure_path, bbox_inches='tight')
