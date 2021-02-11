import json
from json import JSONDecodeError
from os.path import exists
from typing import Optional, Tuple, List

import dgl
import torch
from omegaconf import DictConfig
from torch.utils.data import Dataset

from utils.common import LABEL, AST, CHILDREN, TOKEN, PAD, NODE, SEPARATOR, UNK
from utils.vocabulary import Vocabulary


class JsonlDataset(Dataset):
    _log_file = "bad_samples.log"

    def __init__(self, data_file: str, vocabulary: Vocabulary, config: DictConfig):
        if not exists(data_file):
            raise ValueError(f"Can't find file with data: {data_file}")
        self._data_file = data_file
        self._vocab = vocabulary
        self._config = config

        self._token_unk = self._vocab.token_to_id[UNK]
        self._node_unk = self._vocab.node_to_id[UNK]
        self._label_unk = self._vocab.label_to_id[UNK]

        self._line_offsets = []
        cumulative_offset = 0
        with open(self._data_file, "r") as file:
            for line in file:
                self._line_offsets.append(cumulative_offset)
                cumulative_offset += len(line.encode(file.encoding))
        self._n_samples = len(self._line_offsets)

    def __len__(self):
        return self._n_samples

    def _read_line(self, index: int) -> str:
        with open(self._data_file, "r") as data_file:
            data_file.seek(self._line_offsets[index])
            line = data_file.readline().strip()
        return line

    def _is_suitable_tree(self, tree: dgl.DGLGraph) -> bool:
        if self._config.max_tree_nodes is not None and tree.number_of_nodes() > self._config.max_tree_nodes:
            return False
        if (
            self._config.max_tree_depth is not None
            and len(dgl.topological_nodes_generator(tree)) > self._config.max_tree_depth
        ):
            return False
        return True

    def __getitem__(self, index) -> Optional[Tuple[torch.Tensor, dgl.DGLGraph]]:
        raw_sample = self._read_line(index)
        try:
            sample = json.loads(raw_sample)
        except JSONDecodeError as e:
            with open(self._log_file, "a") as log_file:
                log_file.write(raw_sample + "\n")
            return None

        # convert label
        label = torch.full((self._config.max_label_parts, 1), self._vocab.label_to_id[PAD])
        sublabels = sample[LABEL].split(SEPARATOR)[: self._config.max_label_parts]
        label[: len(sublabels), 0] = torch.tensor(
            [self._vocab.label_to_id.get(sl, self._label_unk) for sl in sublabels]
        )

        # iterate through nodes
        ast = sample[AST]
        node_to_parent = {}
        nodes: List[Tuple[str, str, Optional[int]]] = []  # list of (subtoken, node, parent)
        for n_id, node in enumerate(ast):
            if CHILDREN in node:
                assert node[TOKEN] == "<EMPTY>", "internal node has non empty token"

                for c in node[CHILDREN]:
                    node_to_parent[c] = len(nodes)

                parent_id = node_to_parent.get(n_id, None)
                nodes.append((node[TOKEN], node[NODE], parent_id))
            else:
                subtokens = node[TOKEN].split(SEPARATOR)[: self._config.max_token_parts]
                parent_id = node_to_parent[n_id]
                nodes += [(st, node[NODE], parent_id) for st in subtokens]

        # convert to dgl graph
        us, vs = zip(*[(child, parent) for child, (_, _, parent) in enumerate(nodes) if parent is not None])
        graph = dgl.graph((us, vs))
        if not self._is_suitable_tree(graph):
            return None
        graph.ndata[TOKEN] = torch.empty((len(nodes),), dtype=torch.long)
        graph.ndata[NODE] = torch.empty((len(nodes),), dtype=torch.long)
        for n_id, (token, node, _) in enumerate(nodes):
            graph.ndata[TOKEN][n_id] = self._vocab.token_to_id.get(token, self._token_unk)
            graph.ndata[NODE][n_id] = self._vocab.node_to_id.get(node, self._node_unk)

        return label, graph

    def _print_tree(self, tree: dgl.DGLGraph, indent: int = 4, symbol: str = "..", indent_ste: int = 4):
        id_to_subtoken = {v: k for k, v in self._vocab.token_to_id.items()}
        id_to_node = {v: k for k, v in self._vocab.node_to_id.items()}
        node_depth = {0: 0}
        print(f"{id_to_subtoken[tree.ndata[TOKEN][0].item()]}/{id_to_node[tree.ndata[NODE][0].item()]}")

        edges = tree.edges()
        for edge_id in dgl.dfs_edges_generator(tree, 0, True):
            edge_id = edge_id.item()
            v, u = edges[0][edge_id].item(), edges[1][edge_id].item()
            cur_depth = node_depth[u] + 1
            node_depth[v] = cur_depth
            print(
                f"{symbol * cur_depth}"
                f"{id_to_subtoken[tree.ndata[TOKEN][v].item()]}/{id_to_node[tree.ndata[NODE][v].item()]}"
            )
