from pickle import load as pkl_load
from typing import Dict, Tuple, Union

import dgl
import dgl.function as fn
import torch
import torch.nn as nn


class _ITreeLSTMCell(nn.Module):
    def __init__(self, x_size: int, h_size: int):
        super().__init__()
        self.x_size = x_size
        self.h_size = h_size

        self.W_iou = nn.Linear(self.x_size, 3 * self.h_size, bias=False)
        self.b_iou = nn.Parameter(torch.zeros(1, 3 * self.h_size), requires_grad=True)

        self.W_f = nn.Linear(self.x_size, self.h_size, bias=False)
        self.b_f = nn.Parameter(torch.zeros((1, h_size)), requires_grad=True)

    def message_func(self, edges: dgl.EdgeBatch) -> Dict:
        raise NotImplementedError

    def reduce_func(self, nodes: dgl.NodeBatch) -> Dict:
        raise NotImplementedError

    def apply_node_func(self, nodes: dgl.NodeBatch) -> Dict:
        iou = nodes.data['x_iou'] + nodes.data['Uh_sum']
        i, o, u = torch.chunk(iou, 3, 1)
        i, o, u = torch.sigmoid(i), torch.sigmoid(o), torch.tanh(u)

        c = i * u + nodes.data['fc_sum']
        h = o * torch.tanh(c)

        return {'h': h, 'c': c}

    def forward(self, graph: dgl.DGLGraph, device: torch.device) -> Tuple[torch.Tensor, torch.Tensor]:
        """Propagate nodes by defined order,
        assuming graph.ndata['x'] contain features
        """
        raise NotImplementedError

    def get_params(self) -> Dict:
        return {
            'w_iou': self.W_iou.weight, 'b_iou': self.b_iou.data,
            'w_f': self.W_f.weight, 'b_f': self.b_f.data
        }

    def _init_matrices(self, graph: dgl.DGLGraph, device: torch.device) -> dgl.DGLGraph:
        number_of_nodes = graph.number_of_nodes()
        graph.ndata['x_iou'] = self.W_iou(graph.ndata['x']) + self.b_iou
        graph.ndata['x_f'] = self.W_f(graph.ndata['x']) + self.b_f
        graph.ndata['h'] = torch.zeros((number_of_nodes, self.h_size), device=device)
        graph.ndata['c'] = torch.zeros((number_of_nodes, self.h_size), device=device)
        graph.ndata['Uh_sum'] = torch.zeros((number_of_nodes, 3 * self.h_size), device=device)
        graph.ndata['fc_sum'] = torch.zeros((number_of_nodes, self.h_size), device=device)
        return graph


class EdgeChildSumTreeLSTMCell(_ITreeLSTMCell):
    """All calculations are happening in message function,
    reduce function only sum children features
    """

    def __init__(self, x_size, h_size):
        super().__init__(x_size, h_size)
        self.U_iou = nn.Linear(self.h_size, 3 * self.h_size, bias=False)
        self.U_f = nn.Linear(self.h_size, self.h_size, bias=False)

    def message_func(self, edges: dgl.EdgeBatch) -> Dict:
        h_f = self.U_f(edges.src['h'])
        x_f = edges.dst['x_f']
        f = torch.sigmoid(x_f + h_f)
        return {
            'Uh': self.U_iou(edges.src['h']),
            'fc': edges.src['c'] * f
        }

    def reduce_func(self, nodes: dgl.NodeBatch) -> Dict:
        """Using builtin functions"""
        raise NotImplementedError

    def apply_node_func(self, nodes: dgl.NodeBatch) -> Dict:
        return super().apply_node_func(nodes)

    def forward(self, graph: dgl.DGLGraph, device: torch.device) -> Tuple[torch.Tensor, torch.Tensor]:
        graph = self._init_matrices(graph, device)

        dgl.prop_nodes_topo(
            graph, reduce_func=[fn.sum('Uh', 'Uh_sum'), fn.sum('fc', 'fc_sum')],
            message_func=self.message_func, apply_node_func=self.apply_node_func
        )

        h = graph.ndata.pop('h')
        c = graph.ndata.pop('c')
        return h, c

    def get_params(self):
        return {
            'w_iou': self.W_iou.weight, 'u_iou': self.U_iou.weight.t(), 'b_iou': self.b_iou.data,
            'w_f': self.W_f.weight, 'u_f': self.U_f.weight.t(), 'b_f': self.b_f.data
        }


class NodeChildSumTreeLSTMCell(_ITreeLSTMCell):
    """All calculations are happening in reduce function
    message function only pass features from source to destination node
    """

    def __init__(self, x_size, h_size):
        super().__init__(x_size, h_size)
        self.U_iou = nn.Linear(self.h_size, 3 * self.h_size, bias=False)
        self.U_f = nn.Linear(self.h_size, self.h_size, bias=False)

    def message_func(self, edges: dgl.EdgeBatch) -> Dict:
        """Using builtin functions"""
        raise NotImplementedError

    def reduce_func(self, nodes: dgl.NodeBatch) -> Dict:
        h_tilda = torch.sum(nodes.mailbox['h'], 1)
        f = torch.sigmoid(self.U_f(nodes.mailbox['h']) + nodes.data['x_f'].unsqueeze(1))
        fc_sum = torch.sum(f * nodes.mailbox['c'], 1)
        return {'Uh_sum': self.U_iou(h_tilda), 'fc_sum': fc_sum}

    def apply_node_func(self, nodes: dgl.NodeBatch) -> Dict:
        return super().apply_node_func(nodes)

    def forward(self, graph: dgl.DGLGraph, device: torch.device) -> Tuple[torch.Tensor, torch.Tensor]:
        graph = self._init_matrices(graph, device)
        # propagate
        dgl.prop_nodes_topo(
            graph, message_func=[fn.copy_u('h', 'h'), fn.copy_u('c', 'c')],
            reduce_func=self.reduce_func, apply_node_func=self.apply_node_func
        )
        # get encoded output
        h = graph.ndata.pop('h')
        c = graph.ndata.pop('c')
        return h, c

    def get_params(self):
        return {
            'w_iou': self.W_iou.weight, 'u_iou': self.U_iou.weight.t(), 'b_iou': self.b_iou.data,
            'w_f': self.W_f.weight, 'u_f': self.U_f.weight.t(), 'b_f': self.b_f.data
        }


class EdgeSpecificTreeLSTMCell(_ITreeLSTMCell):

    def __init__(self, x_size, h_size, type_relationship: Union[str, Dict]):
        """Set matrix for each edge base on types of vertices
        :param type_relationship: if str, then path to pickle with Dict
        key: tuple with ids for src group, e.g. If and Switch statements
        value: list of lists, where each list corresponding to type ids of some group
        { ...
            (src_type_id_1, ..., src_type_id_n): [
                [dst_group_type_id_1, ..., dst_group_type_id_k],
                ...,
                [dst_group_type_id_1, ..., dst_group_type_id_m]
            ]
        ... }
        """
        super().__init__(x_size, h_size)
        if isinstance(type_relationship, str):
            with open(type_relationship, 'rb') as pkl_file:
                self.type_relationship = pkl_load(pkl_file)
        else:
            self.type_relationship = type_relationship
        count_diff_matrix = 1
        # dict of matrices ids, key: (src_type_id, dst_type_id), value: matrix_id
        self.edge_matrix_id = {}
        for type_ids, groups in self.type_relationship.items():
            for dst_group in groups:
                for child_id in dst_group:
                    for src_id in type_ids:
                        self.edge_matrix_id[(src_id, child_id)] = count_diff_matrix
                count_diff_matrix += 1

        self.U_iou = nn.Parameter(torch.rand(count_diff_matrix, self.h_size, 3 * self.h_size), requires_grad=True)
        self.U_f = nn.Parameter(torch.rand(count_diff_matrix, self.h_size, self.h_size), requires_grad=True)

    def message_func(self, edges: dgl.EdgeBatch) -> Dict:
        _U_iou = self.U_iou[edges.data['matrix_id']]
        _x = edges.src['h'].unsqueeze(1)
        _Uh = torch.bmm(_x, _U_iou).squeeze(1)

        _U_f = self.U_f[edges.data['matrix_id']]
        h_f = torch.bmm(_x, _U_f).squeeze(1)
        f = torch.sigmoid(edges.dst['x_f'] + h_f)
        return {
            'Uh': _Uh,
            'fc': edges.src['c'] * f
        }

    def reduce_func(self, nodes: dgl.NodeBatch) -> Dict:
        return super().reduce_func(nodes)

    def apply_node_func(self, nodes: dgl.NodeBatch) -> Dict:
        return super().apply_node_func(nodes)

    def forward(self, graph: dgl.DGLGraph, device: torch.device) -> Tuple[torch.Tensor, torch.Tensor]:
        matrix_id = torch.zeros(graph.number_of_edges(), dtype=torch.long)
        # because all edges in graph reversed (from child to parent)
        for edge_dst, edge_src, edge_id in zip(*graph.all_edges('all')):
            src_type = graph.ndata['type_id'][edge_src].item()
            dst_type = graph.ndata['type_id'][edge_dst].item()
            matrix_id[edge_id.item()] = self.edge_matrix_id.get((src_type, dst_type), 0)
        graph.edata['matrix_id'] = matrix_id.to(device)

        graph = self._init_matrices(graph, device)
        dgl.prop_nodes_topo(
            graph, reduce_func=[fn.sum('Uh', 'Uh_sum'), fn.sum('fc', 'fc_sum')],
            message_func=self.message_func, apply_node_func=self.apply_node_func
        )
        h = graph.ndata.pop('h')
        c = graph.ndata.pop('c')
        return h, c

    def get_params(self) -> Dict:
        return {
            'w_iou': self.W_iou.weight, 'u_iou': self.U_iou.data, 'b_iou': self.b_iou.data,
            'w_f': self.W_f.weight, 'u_f': self.U_f.data, 'b_f': self.b_f.data
        }


class TypeSpecificTreeLSTMCell(_ITreeLSTMCell):

    def __init__(self, x_size: int, h_size: int, nary_types: Union[str, Dict]):
        """Use NAry cell type for given types
        :param nary_types: if str, then path to pickle with Dict
        contains information about types for NAry equations:
        { ...
            src_type_id: [
                ...
                [dst_type_id_1, ..., dst_type_id_k],
                ...
            ]
        ... }
        """
        super().__init__(x_size, h_size)
        if isinstance(nary_types, str):
            with open(nary_types, 'rb') as pkl_file:
                self.nary_types = pkl_load(pkl_file)
        else:
            self.nary_types = nary_types
        count_diff_matrix = 1
        # dict of matrices ids, key: src_type_id, value: {(dst_type_id_1, ..., dst_type_id_k): (m_id_1, ..., m_id_k))}
        self.edge_matrix_id = {}
        for src_type_id, dst_type_ids in self.nary_types.items():
            if src_type_id not in self.edge_matrix_id:
                self.edge_matrix_id[src_type_id] = {}
            cur_indexes = torch.tensor(range(count_diff_matrix, count_diff_matrix + len(dst_type_ids[0])))
            for dst_type_id in dst_type_ids:
                self.edge_matrix_id[src_type_id][tuple(sorted(dst_type_id))] = cur_indexes
            count_diff_matrix += len(dst_type_ids[0])

        self.U_iou = nn.Parameter(torch.rand(count_diff_matrix, self.h_size, 3 * self.h_size), requires_grad=True)
        self.U_f = nn.Parameter(torch.rand(count_diff_matrix, self.h_size, self.h_size), requires_grad=True)

    def message_func(self, edges: dgl.EdgeBatch) -> Dict:
        _U_iou = self.U_iou[edges.data['matrix_id']]
        _x = edges.src['h'].unsqueeze(1)
        _Uh = torch.bmm(_x, _U_iou).squeeze(1)

        _U_f = self.U_f[edges.data['matrix_id']]
        h_f = torch.bmm(_x, _U_f).squeeze(1)
        f = torch.sigmoid(edges.dst['x_f'] + h_f)
        return {
            'Uh': _Uh,
            'fc': edges.src['c'] * f
        }

    def reduce_func(self, nodes: dgl.NodeBatch) -> Dict:
        return super().reduce_func(nodes)

    def apply_node_func(self, nodes: dgl.NodeBatch) -> Dict:
        return super().apply_node_func(nodes)

    def forward(self, graph: dgl.DGLGraph, device: torch.device) -> Tuple[torch.Tensor, torch.Tensor]:
        matrix_id = torch.zeros(graph.number_of_edges(), dtype=torch.long)
        # because all edges in graph reversed (from child to parent)
        for node in range(graph.number_of_nodes()):
            children, _, edge_ids = graph.in_edges(node, form='all')
            children_ids = tuple(sorted(graph.ndata['type_id'][children].tolist()))
            if node in self.edge_matrix_id:
                if children_ids in self.edge_matrix_id[node]:
                    matrix_id[edge_ids] = self.edge_matrix_id[node][children_ids]

        graph.edata['matrix_id'] = matrix_id.to(device)
        graph = self._init_matrices(graph, device)

        dgl.prop_nodes_topo(
            graph, reduce_func=[fn.sum('Uh', 'Uh_sum'), fn.sum('fc', 'fc_sum')],
            message_func=self.message_func, apply_node_func=self.apply_node_func
        )

        h = graph.ndata.pop('h')
        c = graph.ndata.pop('c')
        return h, c

    def get_params(self) -> Dict:
        return {
            'w_iou': self.W_iou.weight, 'u_iou': self.U_iou.data, 'b_iou': self.b_iou.data,
            'w_f': self.W_f.weight, 'u_f': self.U_f.data, 'b_f': self.b_f.data
        }


class LuongAttentionTreeLSTMCell(_ITreeLSTMCell):

    def __init__(self, x_size, h_size):
        super().__init__(x_size, h_size)
        self.U_iou = nn.Linear(self.h_size, 3 * self.h_size, bias=False)
        self.U_f = nn.Linear(self.h_size, self.h_size, bias=False)

        self.W_a = nn.Linear(self.x_size + self.h_size, self.h_size, bias=False)
        self.v_a = nn.Linear(self.h_size, 1, bias=False)

    def message_func(self, edges: dgl.EdgeBatch) -> Dict:
        """use built-in functions"""
        raise NotImplementedError

    def reduce_func(self, nodes: dgl.NodeBatch) -> Dict:
        # [bs; n children; h size]
        h_children = nodes.mailbox['h']

        # [bs; n children; x size]
        x = nodes.data['x'].unsqueeze(1).expand(-1, h_children.shape[1], -1)

        # [bs; n children; h size]
        energy = torch.tanh(self.W_a(
            # [bs; n children; x size + h size]
            torch.cat([x, h_children], dim=2)
        ))

        # [bs; n children]
        scores = self.v_a(energy).squeeze(2)

        # [bs; n children]
        align = nn.functional.softmax(scores, dim=1)

        # [bs; h size]
        h_attn = torch.bmm(align.unsqueeze(1), h_children).squeeze(1)

        # [bs; n children; h size]
        f = torch.sigmoid(self.U_f(nodes.mailbox['h']) + nodes.data['x_f'].unsqueeze(1))
        # [bs; h size]
        fc_sum = torch.sum(f * nodes.mailbox['c'], 1)

        return {
            'Uh_sum': self.U_iou(h_attn),  # name for using with super functions
            'fc_sum': fc_sum
        }

    def apply_node_func(self, nodes: dgl.NodeBatch) -> Dict:
        return super().apply_node_func(nodes)

    def forward(self, graph: dgl.DGLGraph, device: torch.device) -> Tuple[torch.Tensor, torch.Tensor]:
        graph = self._init_matrices(graph, device)

        dgl.prop_nodes_topo(
            graph, reduce_func=self.reduce_func, apply_node_func=self.apply_node_func,
            message_func=[fn.copy_u('h', 'h'), fn.copy_u('c', 'c')]
        )

        h = graph.ndata.pop('h')
        c = graph.ndata.pop('c')
        return h, c

    def get_params(self):
        return {
            'w_iou': self.W_iou.weight, 'u_iou': self.U_iou.weight, 'b_iou': self.b_iou.data,
            'w_f': self.W_f.weight, 'u_f': self.U_f.weight.t(), 'b_f': self.b_f.data,
            'w_query': self.W_query.weight, 'w_key': self.W_key.weight, 'w_value': self.W_value.weight
        }


class MultiHeadAttentionTreeLSTMCell(_ITreeLSTMCell):

    def __init__(self, x_size: int, h_size: int, n_heads: int, dropout: float = 0):
        super().__init__(x_size, h_size)
        assert x_size == h_size
        self.multihead_attention = nn.MultiheadAttention(x_size, n_heads, dropout)

        self.U_iou = nn.Linear(self.h_size, 3 * self.h_size)
        self.U_f = nn.Linear(self.h_size, self.h_size)

    def message_func(self, edges: dgl.EdgeBatch) -> Dict:
        """use built-in functions"""
        raise NotImplementedError

    def reduce_func(self, nodes: dgl.NodeBatch) -> Dict:
        # [1, bs, x size]
        query = nodes.data['x'].unsqueeze(0)
        # [n children, bs, h size]
        key_value = nodes.mailbox['h']

        # [bs, h size]
        h_attn = self.multihead_attention(query, key_value, key_value)[0].squeeze(0)

        # [bs; 3 * h_size]
        h_iou = self.U_iou(h_attn)
        # [bs; h_size]
        h_f = self.U_f(h_attn)

        f = torch.sigmoid(nodes.data['x_f'] + h_f).unsqueeze(1)
        fc = nodes.mailbox['c'] * f
        return {
            'Uh_sum': h_iou,  # name for using with super functions
            'fc_sum': torch.sum(fc, 1)
        }

    def apply_node_func(self, nodes: dgl.NodeBatch) -> Dict:
        return super().apply_node_func(nodes)

    def forward(self, graph: dgl.DGLGraph, device: torch.device) -> Tuple[torch.Tensor, torch.Tensor]:
        graph = self._init_matrices(graph, device)

        dgl.prop_nodes_topo(
            graph, reduce_func=self.reduce_func, apply_node_func=self.apply_node_func,
            message_func=[fn.copy_u('h', 'h'), fn.copy_u('x', 'x'), fn.copy_u('c', 'c')]
        )

        h = graph.ndata.pop('h')
        c = graph.ndata.pop('c')
        return h, c

    def get_params(self) -> Dict:
        params = super().get_params()
        params.update({
            'w_query': self.W_query.weight, 'w_query_bias': self.W_query.bias,
            'w_key': self.W_key.weight, 'w_key_bias': self.W_key.bias,
            'w_value': self.W_value.weight, 'w_value_bias': self.W_value.bias,
            'w_linear': self.attn_linear.weight, 'b_linear': self.attn_linear.bias,
            'u_iou_w': self.U_iou.weight, 'u_f_w': self.U_f.weight,
            'u_iou_b': self.U_iou.bias, 'u_f_b': self.U_f.bias
        })
        return params
