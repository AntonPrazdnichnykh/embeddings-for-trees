from argparse import ArgumentParser
from copy import deepcopy
from json import load as json_load
from pickle import load as pkl_load
from typing import Dict

import torch
import torch.nn as nn
from tqdm.auto import tqdm

from data_workers.dataset import JavaDataset
from model.tree2seq import ModelFactory, Tree2Seq
from utils.common import fix_seed, get_device, split_tokens_to_subtokens, PAD, UNK, is_current_step_match
from utils.learning_info import LearningInfo
from utils.logging import get_possible_loggers, FileLogger, WandBLogger, FULL_DATASET, TerminalLogger
from utils.training import train_on_batch, evaluate_dataset


def train(params: Dict, logging: str) -> None:
    fix_seed()
    device = get_device()
    print(f"using {device} device")

    training_set = JavaDataset(params['paths']['train'], params['batch_size'], True)
    validation_set = JavaDataset(params['paths']['validate'], params['batch_size'], True)

    print('processing labels...')
    with open(params['paths']['labels'], 'rb') as pkl_file:
        label_to_id = pkl_load(pkl_file)
    sublabel_to_id, label_to_sublabel = split_tokens_to_subtokens(label_to_id, device=device)

    print('processing vocabulary...')
    with open(params['paths']['vocabulary'], 'rb') as pkl_file:
        vocabulary = pkl_load(pkl_file)
        token_to_id = vocabulary['token_to_id']
        type_to_id = vocabulary['type_to_id']
        label_to_id = vocabulary['label_to_id']

    print('model initializing...')
    # create models
    extended_params = deepcopy(params)
    extended_params['decoder']['params']['out_size'] = len(sublabel_to_id)
    extended_params['decoder']['params']['padding_index'] = sublabel_to_id[PAD]

    model_factory = ModelFactory(
        extended_params['embedding'], extended_params['encoder'], extended_params['decoder'],
        params['hidden_states'], token_to_id, type_to_id, label_to_id
    )
    model: Tree2Seq = model_factory.construct_model(device)

    # create optimizer
    optimizer = torch.optim.Adam(
        model.parameters(), lr=params['lr'], weight_decay=params['weight_decay']
    )

    # define loss function
    criterion = nn.CrossEntropyLoss(ignore_index=sublabel_to_id[PAD]).to(device)

    # init logging class
    logger = None
    if logging == TerminalLogger.name:
        logger = TerminalLogger(params['checkpoints_folder'])
    elif logging == FileLogger.name:
        logger = FileLogger(params, params['logging_folder'], params['checkpoints_folder'])
    elif logging == WandBLogger.name:
        logger = WandBLogger('treeLSTM', params, model, params['checkpoints_folder'])

    # train loop
    print("ok, let's train it")
    for epoch in range(params['n_epochs']):
        train_acc_info = LearningInfo()

        # iterate over training set
        for batch_id in tqdm(range(len(training_set))):
            graph, labels = training_set[batch_id]
            graph.ndata['token_id'] = graph.ndata['token_id'].to(device)
            graph.ndata['type_id'] = graph.ndata['type_id'].to(device)
            batch_info = train_on_batch(
                model, criterion, optimizer, graph, labels,
                label_to_sublabel, sublabel_to_id, params, device
            )
            train_acc_info.accumulate_info(batch_info)
            if is_current_step_match(batch_id, params['logging_step']):
                logger.log(train_acc_info.get_state_dict(), epoch, batch_id)
                train_acc_info = LearningInfo()
            if is_current_step_match(batch_id, params['evaluation_step']):
                eval_epoch_info = evaluate_dataset(validation_set, model, criterion, sublabel_to_id, device)
                logger.log(eval_epoch_info.get_state_dict(), epoch, FULL_DATASET, False)

        # iterate over validation set
        eval_epoch_info = evaluate_dataset(validation_set, model, criterion, sublabel_to_id, device)
        logger.log(eval_epoch_info.get_state_dict(), epoch, FULL_DATASET, False)

        if is_current_step_match(epoch, params['checkpoint_step']):
            logger.save_model(model, epoch, extended_params)


if __name__ == '__main__':
    arg_parse = ArgumentParser()
    arg_parse.add_argument('--config', type=str, required=True, help='path to config json')
    arg_parse.add_argument('--logging', choices=get_possible_loggers(), required=True)
    args = arg_parse.parse_args()

    with open(args.config) as config_file:
        train(json_load(config_file), args.logging)
