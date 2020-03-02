from typing import Dict, Tuple, List

import torch
import torch.nn as nn
from dgl import BatchedDGLGraph

from model.decoder import _IDecoder, LinearDecoder, LSTMDecoder
from model.embedding import _IEmbedding, FullTokenEmbedding, SubTokenEmbedding, SubTokenTypeEmbedding, \
    PositionalSubTokenTypeEmbedding
from model.encoder import _IEncoder
from model.transformer_encoder import Transformer
from model.treeLSTM import TokenTreeLSTM, TokenTypeTreeLSTM, LinearTreeLSTM, SumEmbedsTreeLSTM


class Tree2Seq(nn.Module):
    def __init__(self, embedding: _IEmbedding, encoder: _IEncoder, decoder: _IDecoder) -> None:
        super().__init__()
        self.embedding = embedding
        self.encoder = encoder
        self.decoder = decoder

    def forward(
            self, graph: BatchedDGLGraph, root_indexes: torch.LongTensor, labels: List[str], device: torch.device
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Predict sequence of tokens for given batched graph

        :param graph: the batched graph
        :param root_indexes: [batch size] indexes of roots in the batched graph
        :param labels: [batch size] string labels of each example
        :param device: torch device
        :return: Tuple[
            logits [the longest sequence, batch size, vocab size]
            ground truth [the longest sequence, batch size]
        ]
        """
        embedded_graph = self.embedding(graph, device)
        encoded_data = self.encoder(embedded_graph, device)
        outputs, ground_truth = self.decoder(encoded_data, labels, root_indexes, device)
        return outputs, ground_truth

    @staticmethod
    def predict(logits: torch.Tensor) -> torch.Tensor:
        """Predict token for each step by given logits

        :param logits: [max length, batch size, number of classes] logits for each position in sequence
        :return: [max length, batch size] token's ids for each position in sequence
        """
        return logits.argmax(dim=-1)


class ModelFactory:
    _embeddings = {
        FullTokenEmbedding.__name__: FullTokenEmbedding,
        SubTokenEmbedding.__name__: SubTokenEmbedding,
        SubTokenTypeEmbedding.__name__: SubTokenTypeEmbedding,
        PositionalSubTokenTypeEmbedding.__name__: PositionalSubTokenTypeEmbedding,
    }
    _encoders = {
        TokenTreeLSTM.__name__: TokenTreeLSTM,
        TokenTypeTreeLSTM.__name__: TokenTypeTreeLSTM,
        LinearTreeLSTM.__name__: LinearTreeLSTM,
        SumEmbedsTreeLSTM.__name__: SumEmbedsTreeLSTM,

        Transformer.__name__: Transformer
    }
    _decoders = {
        LinearDecoder.__name__: LinearDecoder,
        LSTMDecoder.__name__: LSTMDecoder,
    }

    def __init__(self, embedding_info: Dict, encoder_info: Dict, decoder_info: Dict,
                 hidden_states: Dict, token_to_id: Dict, type_to_id: Dict, label_to_id: Dict):
        self.embedding_info = embedding_info
        self.encoder_info = encoder_info
        self.decoder_info = decoder_info

        self.hidden_states = hidden_states

        self.token_to_id = token_to_id
        self.type_to_id = type_to_id
        self.label_to_id = label_to_id

        self.embedding = self._get_module(self.embedding_info['name'], self._embeddings)
        self.encoder = self._get_module(self.encoder_info['name'], self._encoders)
        self.decoder = self._get_module(self.decoder_info['name'], self._decoders)

    @staticmethod
    def _get_module(module_name: str, modules_dict: Dict) -> nn.Module:
        if module_name not in modules_dict:
            raise ModuleNotFoundError(f"Unknown module {module_name}, try one of {', '.join(modules_dict.keys())}")
        return modules_dict[module_name]

    def construct_model(self, device: torch.device) -> Tree2Seq:
        return Tree2Seq(
            self.embedding(
                h_emb=self.hidden_states['embedding'], token_to_id=self.token_to_id,
                type_to_id=self.type_to_id, **self.embedding_info['params']
            ),
            self.encoder(
                h_emb=self.hidden_states['embedding'], h_enc=self.hidden_states['encoder'],
                **self.encoder_info['params']
            ),
            self.decoder(
                h_enc=self.hidden_states['encoder'], h_dec=self.hidden_states['decoder'],
                label_to_id=self.label_to_id, **self.decoder_info['params']
            )
        ).to(device)

    def save_configuration(self) -> Dict:
        return {
            'embedding_info': self.embedding_info,
            'encoder_info': self.encoder_info,
            'decoder_info': self.decoder_info,
            'hidden_states': self.hidden_states,
            'token_to_id': self.token_to_id,
            'type_to_id': self.type_to_id,
            'label_to_id': self.label_to_id
        }


def load_model(path_to_model: str, device: torch.device) -> Tuple[Tree2Seq, Dict]:
    checkpoint: Dict = torch.load(path_to_model, map_location=device)
    configuration = checkpoint['configuration']
    model_factory = ModelFactory(**configuration)
    model: Tree2Seq = model_factory.construct_model(device)
    model.load_state_dict(checkpoint['state_dict'])
    return model, checkpoint
