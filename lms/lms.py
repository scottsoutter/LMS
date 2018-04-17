"""LMS class for Large Model Support."""

import tensorflow as tf
import tensorflow.contrib.graph_editor as ge
from tensorflow.contrib.graph_editor import util

import time
import queue as Queue
import lms.topos as topos
from enum import Enum, auto


class CTRLD_Strategy(Enum):
    CHAIN_RULE = auto()
    DIRECT_ORDER = auto()


class LMS(object):
    """LMS class for Large Model Support (LMS).

    The `LMS` object statically modifies a model by swapping its tensors
    to the host so that the model can be trained with the limited memory
    of GPUs.

    Tensors those are generated by forward operations and consumed by
    backward operations are candidates for swapping. The `LMS` object will
    automatically find these tensors.

    Swapping is done by cutting the link between a forward operation and
    its backward operation, then replacing the link by inserting `identity`
    operations on the host. In theory, this procedure does not have any
    effect on the training convergence as well as inference task.
    """

    def __init__(self, graph=None, optimizer_scopes=set(),
                 starting_scope=None,
                 excl_scopes=set(),
                 incl_scopes=set(),
                 excl_types=set(),
                 incl_types=set(),
                 lb=1, ub=10000,
                 n_tensors=-1,
                 fuse_swapins=False,
                 ctrld_strategy="chain_rule",
                 swap_branches=False,
                 branch_threshold=0,
                 debug=False,
                 debug_level=1,
                 cpu_device="/cpu:0"):
        if optimizer_scopes is None:
            self.log_info("set the optimizer scope")
            return

        self.graph = graph
        self.optimizer_scopes = optimizer_scopes
        self.excl_scopes = excl_scopes
        self.incl_scopes = incl_scopes
        self.excl_types = excl_types
        self.incl_types = incl_types
        self.starting_scope = starting_scope
        self.lb = lb  # lowerbound
        self.ub = ub  # upperbound
        self.n_tensors = n_tensors
        self.fuse_swapins = fuse_swapins
        if ctrld_strategy == "chain_rule":
            self.ctrld_strategy = CTRLD_Strategy.CHAIN_RULE
        elif ctrld_strategy == "direct_order":
            self.ctrld_strategy = CTRLD_Strategy.DIRECT_ORDER
        else:
            self.ctrld_strategy = "chain_rule"

        self._swap_branches = swap_branches
        self._branch_threshold = branch_threshold

        # Operations with these types will be ignored
        atomic_types = {'Const', 'Mul', 'Add',
                        'Identity', 'Assign', 'VariableV2',
                        'Reshape', 'Shape', 'ShapeN'}
        self.excl_types |= atomic_types

        self.excl_ops = set()
        self.incl_ops = set()
        self.grad_ops = set()
        self.topo_sort = None
        self.cpu_device = cpu_device
        self.debug = debug
        self.debug_level = debug_level

        # keep log of tensors on host
        self.incpu_count = 0

    def run(self, graph=None):
        if graph is not None:
            self.graph = graph

        if self.graph is None:
            self.log_info("Input graph: Not found")
            return

        if self.n_tensors == 0:
            self.log_info("Not modify model for LMS")
            return  # turn off LMS
        elif self.n_tensors < 0:
            self.n_tensors = 0  # swap all tensors (default)

        self.log_info("Editing model for LMS")
        self.print_configuration()
        start_time = time.time()

        # gradient ops
        for scope in self.optimizer_scopes:
            self.grad_ops.update(
                set(ge.filter_ops_from_regex(
                    ge.make_list_of_op(self.graph), "^{}".format(scope))))

        # seep ops for search
        seed_ops = None
        if self.starting_scope is not None:
            seed_ops = ge.filter_ops_from_regex(
                ge.make_list_of_op(self.graph), "^{}".format(
                    self.starting_scope))
        else:
            candidates = set()
            for op in self.graph.get_operations():
                if op in self.grad_ops:
                    continue
                for t in op.outputs:
                    frontier_ops = set(util.get_consuming_ops(t))
                    if (frontier_ops & self.grad_ops):
                        candidates.add(op)
                        break

            # ordering an operation by how much it covers the other ops
            tmp_dict = {}
            sorted_list = []
            for op in candidates:
                nelems = len(set(ge.get_forward_walk_ops(op, inclusive=False))
                             & candidates)
                if nelems > 0:
                    tmp_dict[op] = nelems
            for key, value in sorted(tmp_dict.items(),
                                     key=lambda x: x[1]):
                sorted_list.append(key)
            # seed ops will cover most of the forward ops
            seed_ops = [sorted_list[-1]]

        self.log_info(
            "Starting ops: {}".format(
                [(op.name, op.type) for op in seed_ops]), 1)

        reachable_ops = set()
        for seed_op in seed_ops:
            reachable_ops |= set(ge.get_forward_walk_ops(seed_op))
        reachable_ops = reachable_ops - self.grad_ops

        # exclusive ops
        for scope in self.excl_scopes:
            self.excl_ops |= set(ge.get_name_scope_ops(reachable_ops, scope))
        self.excl_ops |= {op
                          for op in reachable_ops
                          if op.type in self.excl_types}

        # inclusive ops
        for scope in self.incl_scopes:
            self.incl_ops |= set(ge.get_name_scope_ops(reachable_ops, scope))
        self.incl_ops |= {op
                          for op in reachable_ops
                          if op.type in self.incl_types}

        # build a topological sort
        self.topo_sort = topos.TOPOS(seed_ops, self.graph, self.grad_ops)
        self.topo_sort.build()
        for i in range(0, self.topo_sort.size):
            self.log_info("[{}]: {}".format(
                i, [op.name for op in self.topo_sort.get_ops(i)]), 1)

        self.do_action(seed_ops)

        # check the validation of the new model
        new_reachable_ops = set()
        for seed_op in seed_ops:
            new_reachable_ops |= set(ge.get_forward_walk_ops(seed_op))
        if (new_reachable_ops >= reachable_ops):
            self.log_info("Edited model is valid and logically equivalent to the original one")
            self.log_info("Added {} ops into the model".format(len(new_reachable_ops - reachable_ops)))
        else:
            self.log_info("Edited model is invalid. Running this may produce unexpected result")

        self.log_info("Editing model for LMS, took: {} ms".format(
            (time.time()-start_time)/1000))
        self.log_info(
            "{} tensors will be swapped out(in) to(from) the host".format(
                self.incpu_count))
        return (new_reachable_ops - reachable_ops)

    def do_action(self, src_ops):  # BFS
        open_set = Queue.Queue()
        closed_set = set()

        for op in src_ops:
            open_set.put(op)

        while not open_set.empty():
            src_op = open_set.get()

            # get next ops before the graph is changed
            next_ops = set()
            for t in src_op.outputs:
                frontier_ops = set(util.get_consuming_ops(t))
                next_ops |= frontier_ops - self.grad_ops

            # do action for src_op
            self.insert_swnodes(src_op)

            for op in next_ops:
                if op in closed_set:
                    continue
                if op not in open_set.queue:
                    open_set.put(op)

            closed_set.add(src_op)

    def insert_swnodes(self, src_op):
        self.log_info("Operation: {}".format(src_op), 2)

        # bypass exclusive ops
        if src_op in self.excl_ops:
            return

        # if inclusive mode is enable
        if self.incl_ops:
            if src_op not in self.incl_ops:
                return

        for t in src_op.outputs:
            if (self.n_tensors > 0) and (self.incpu_count >= self.n_tensors):
                return

            frontier_ops = set(util.get_consuming_ops(t))
            self.log_info("my frontier ops: {}".format(frontier_ops), 2)

            bw_frontier_ops = frontier_ops & self.grad_ops
            self.log_info("my bw frontier ops: {}".format(bw_frontier_ops), 2)

            # swap branch ops if they are far enough (depending on threshold)
            if self._swap_branches:
                fw_branch_ops = self._get_branch_ops(
                    frontier_ops - self.grad_ops,
                    self._branch_threshold)
                bw_frontier_ops = bw_frontier_ops | fw_branch_ops

            # Not swap tensors used by bw ops without outgoing ops.
            # These bw ops can be removed by Tensorflow compiler
            bw_frontier_ops = {op
                               for op in bw_frontier_ops
                               if ge.get_forward_walk_ops(op, inclusive=False)}

            if not bw_frontier_ops:
                continue

            self.log_info("Operation: {}, order {}, type {}".format(
                src_op.name, self.topo_sort.get_order(src_op),
                src_op.type), 1)

            # create swap_out node
            sample_op = next(iter(bw_frontier_ops))
            swapout_op = self.add_swapout(src_op, sample_op)
            self.incpu_count = self.incpu_count + 1

            # create swap_in nodes
            # TODO: swap_in nodes for branches
            if self.fuse_swapins:
                fuse_bw_frontier_ops = {
                    op for op in bw_frontier_ops
                    if self.topo_sort.get_order(op) > 0}
                if fuse_bw_frontier_ops:
                    ts = ge.filter_ts_from_regex(sample_op, src_op.name)
                    with tf.device(self.cpu_device):
                        swap_in = tf.identity(ts[0])

                    # Connect: swap_out -> swap_in
                    connect_ops(swapout_op, swap_in.op, self.graph)
                    self.excl_ops.add(swap_in.op)

                    # reuse swap_in tensors
                    for op in fuse_bw_frontier_ops:
                        ts = ge.filter_ts_from_regex(op, src_op.name)
                        # Connect: swap_in -> dest
                        input_idx = ge.sgv(
                            op, graph=self.graph).input_index(ts[0])
                        connect_ops(swap_in.op, op, self.graph,
                                    remap_inputs=True, idx=input_idx)

                        self.log_info(
                            "{} (order {}) reuses tensor {}".format(
                                op.name,
                                self.topo_sort.get_order(op),
                                ts[0].name),
                            1)

                    # control dependency -> swap_in
                    min_order = self.topo_sort.size + 1
                    earliest_op = None
                    for op in fuse_bw_frontier_ops:
                        order = self.topo_sort.get_order(op)
                        if order < min_order:
                            min_order = order
                            earliest_op = op
                    if earliest_op:
                        self.add_ctrld(src_op, earliest_op, swap_in.op,
                                       self.lb, self.ub)
                    bw_frontier_ops -= fuse_bw_frontier_ops

            for dest_op in bw_frontier_ops:
                # swap_in op
                swapin_op = self.add_swapin(swapout_op, src_op, dest_op)
                # control dependency -> swap_in
                self.add_ctrld(src_op, dest_op, swapin_op, self.lb, self.ub)

    def _get_branch_ops(self, within_ops, threshold=0):
        orders = {self.topo_sort.get_order(op)
                  for op in within_ops}
        if not orders:
            return set()
        min_order = min(orders) + threshold
        branch_ops = {
            op
            for op in within_ops
            if (self.topo_sort.get_order(op) > min_order)}
        return branch_ops

    def add_swapout(self, src_op, dest_op):
        ts = ge.filter_ts_from_regex(dest_op, src_op.name)
        ts0 = ts[0]

        with tf.device(self.cpu_device):
            swap_out = tf.identity(ts0)

        # Connect: src-node -> swap-out
        src_out_idx = ge.sgv(src_op, graph=self.graph).output_index(ts0)
        connect_ops(src_op, swap_out.op, self.graph,
                    remap_outputs=True, idx=src_out_idx)
        self.excl_ops.add(swap_out.op)
        self.log_info("Tensor {} will be placed on {}".format(
            ts0.name, self.cpu_device), 1)

        return swap_out.op

    def add_swapin(self, swapout_op, src_op, dest_op):
        ts = ge.filter_ts_from_regex(dest_op, src_op.name)
        ts0 = ts[0]

        with tf.device(self.cpu_device):
            swap_in = tf.identity(ts0)

        # Connect: swap_out -> swap_in
        connect_ops(swapout_op, swap_in.op, self.graph)

        # Connect: swap_in -> dest
        input_idx = ge.sgv(dest_op, graph=self.graph).input_index(ts[0])
        connect_ops(swap_in.op, dest_op, self.graph,
                    remap_inputs=True, idx=input_idx)
        self.excl_ops.add(swap_in.op)

        self.log_info("Consuming op {} (order {}) swaps in {}".format(
            dest_op.name, self.topo_sort.get_order(dest_op),
            ts0.name), 1)

        return swap_in.op

    def add_ctrld(self, fw_op, bw_op, swapin_op, lb, ub):
        if self.topo_sort.get_order(bw_op) < 0:
            nco = self.find_nco(fw_op, bw_op)
            if nco:
                bw_op = nco
            else:
                in_scope_ops = self.find_inscope(bw_op.name)
                if in_scope_ops:
                    bw_op = in_scope_ops
                else:
                    self.log_info("No control dependency op", 1)
                    return

        # if lb is out of range, reset it to make sure
        # that a control dependency op will be found
        if (self.topo_sort.get_order(bw_op) - lb 
            <= self.topo_sort.get_order(fw_op)):
            lb = 1
        if self.ctrld_strategy is CTRLD_Strategy.CHAIN_RULE:
            re = self.do_chain_rule(fw_op, bw_op, lb, ub)
        elif self.ctrld_strategy is CTRLD_Strategy.DIRECT_ORDER:
            re = self.do_direct_order(fw_op, bw_op, lb, ub)
        else:
            re = self.do_chain_rule(fw_op, bw_op, lb, ub)

        ctrld_op = re[0]
        ctrld_order = re[1]
        if ctrld_op:
            ge.add_control_inputs(swapin_op, ctrld_op)
            self.log_info(
                "Control dependency op {},  order: {}".format(
                    ctrld_op.name, ctrld_order), 1)
        else:
            self.log_info("No control dependency op", 1)

    def find_nco(self, fw_op, bw_op):
        '''Find the nearest common ops in reachable ops of two given ops
        '''
        frontier_ops = set()
        for t in fw_op.outputs:
            frontier_ops |= set(util.get_consuming_ops(t))
        frontier_ops -= self.grad_ops
        fw_reachable_ops = {op2
                            for op1 in frontier_ops
                            for op2 in set(ge.get_forward_walk_ops(op1))}

        bw_reachable_ops = set(ge.get_forward_walk_ops(bw_op, inclusive=False))
        common_ops = fw_reachable_ops & bw_reachable_ops
        min_order = self.topo_sort.size + 1
        nco_op = None
        for op in common_ops:
            order = self.topo_sort.get_order(op)
            if order < 0:
                continue
            if order < min_order:
                min_order = order
                nco_op = op
        return nco_op

    def find_inscope(self, scope):
        current_scope = scope
        higher_scope = current_scope.rsplit('/', 1)[0]

        visited_ops = set()
        while (current_scope != higher_scope):
            ops = set(ge.filter_ops_from_regex(
                ge.make_list_of_op(self.graph),
                "^{}".format(higher_scope)))

            # not consider inner ops
            ops1 = ops - visited_ops

            # gradient ops only
            ops1 &= self.grad_ops

            # ops in chain rule
            ops1 = {op for op in ops1 if self.topo_sort.get_order(op) > 0}

            # get the earliest op
            min_order = self.topo_sort.size + 1
            earliest_op = None
            for op in ops1:
                order = self.topo_sort.get_order(op)
                if order < min_order:
                    min_order = order
                    earliest_op = op
            if not earliest_op:
                # go outside
                visited_ops |= ops
                current_scope = higher_scope
                higher_scope = current_scope.rsplit('/', 1)[0]
            else:
                return earliest_op

    def do_chain_rule(self, fw_op, bw_op, lower_b, upper_b):  # BFS
        '''Find a control dependency operation using chain rules.
        Go down along the forward phase to find corresponding bw ops
        '''
        fw_order = self.topo_sort.get_order(fw_op)
        bw_order = self.topo_sort.get_order(bw_op)

        # check if the bw op is near the boundary between fw and bw phases
        if (bw_order - lower_b) < self.topo_sort.bw_starting_order:
            return self.do_direct_order(fw_op, bw_op, lower_b, upper_b)

        open_set1 = Queue.Queue()
        open_set2 = Queue.Queue()
        closed_set = set()

        open_set1.put(fw_op)

        result_ops = set()
        while not open_set1.empty():
            # stop if reaching the upperbound
            if upper_b == 0 or (lower_b > upper_b):
                break

            src_op = open_set1.get()

            # do action for src_op
            total_consumming_ops = set()
            for t in src_op.outputs:
                consumming_ops = set(util.get_consuming_ops(t))
                total_consumming_ops |= consumming_ops

            if lower_b <= 0:
                # inside the range
                consumming_ops_bw = total_consumming_ops & self.grad_ops
                # check validation
                consumming_ops_bw = {
                    op
                    for op in consumming_ops_bw
                    if self.topo_sort.get_order(op) > fw_order}
                consumming_ops_bw = {
                    op
                    for op in consumming_ops_bw
                    if self.topo_sort.get_order(op) < bw_order}
                result_ops |= consumming_ops_bw
            # go to the next level
            next_ops = total_consumming_ops - self.grad_ops
            for op in next_ops:
                if op in closed_set:
                    continue
                if op not in open_set2.queue:
                    open_set2.put(op)

            closed_set.add(src_op)
            if open_set1.empty():
                if result_ops:
                    break
                lower_b = lower_b - 1
                upper_b = upper_b - 1
                while not open_set2.empty():
                    open_set1.put(open_set2.get())
        if result_ops:
            ctrld_op = next(iter(result_ops))
            return (ctrld_op, self.topo_sort.get_order(ctrld_op))
        else:
            return (None, -1)

    def do_direct_order(self, fw_op, src_op, lower_b, upper_b):
        '''Find a control dependency operation using topological sort
        '''
        result_ops = set()

        # offset ordering
        fw_order = self.topo_sort.get_order(fw_op)
        src_order = self.topo_sort.get_order(src_op)

        range_ub = src_order - lower_b
        range_lb = max([src_order - upper_b, fw_order]) + 1

        ctrld_order = -1
        for i in reversed(range(range_lb, range_ub)):
            candidates = self.topo_sort.get_ops(i)
            # on the chain rule path
            candidates = {op
                          for op in candidates
                          if src_op in ge.get_forward_walk_ops(op)}
            if candidates:
                result_ops |= candidates
                ctrld_order = i
                break

        if result_ops:
            ctrld_op = next(iter(result_ops))
            return (ctrld_op, ctrld_order)
        else:
            return (None, -1)

    def log_info(self, message, level=0):
        if level == 0 or (self.debug and self.debug_level >= level):
            # Use tf.logging.info instead of print, since print
            # is not thread safe, which can break tests.
            tf.logging.info("[LMS][{}] {}".format(level, message))

    def print_configuration(self):
        if self.n_tensors == 0:
            self.log_info("n_tensors: all tensors")
        else:
            self.log_info("n_tensors: {}".format(self.n_tensors))
        self.log_info("lb: {}".format(self.lb))


def connect_ops(src_op, dest_op, graph,
                remap_inputs=False, remap_outputs=False,
                idx=None, disconnect_first=False):
    src_sgv = ge.sgv(src_op, graph=graph)
    dest_sgv = ge.sgv(dest_op, graph=graph)
    if remap_outputs:
        src_sgv = src_sgv.remap_outputs([idx])
    if remap_inputs:
        dest_sgv = dest_sgv.remap_inputs([idx])

    ge.connect(src_sgv, dest_sgv, disconnect_first)
