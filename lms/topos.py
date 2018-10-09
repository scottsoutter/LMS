# (C) Copyright IBM Corp. 2018. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""TOPOS
"""
from lms import util as ut


class TOPOS(object):
    """TOPOS class builds a topological sort from the computational graph.
    """
    def __init__(self, graph):
        """Create a TOPOS object.

        Args:
          seed_ops: a list of `tf.Operation`.
        """
        self._graph = graph
        self._topo_sort = []
        self._levels = {}

    def build(self):
        """Build a categorized topological sort
        """
        dep_dict = {}
        for op in self._graph.get_operations():
            dep_dict[op] = ut.fanins(op) | set(op.control_inputs)

        # build a categorized topological sort
        while True:
            current_level_ops = {item for item, dep in dep_dict.items()
                                 if len(dep) == 0}
            if not current_level_ops:
                break
            else:
                self._topo_sort.append(current_level_ops)
                dep_dict = {item: (dep - current_level_ops)
                            for item, dep in dep_dict.items()
                            if item not in current_level_ops}

        # build a dict of (op, level)
        for i in range(0, len(self._topo_sort)):
            for op in self._topo_sort[i]:
                self._levels[op] = i

    def reset(self):
        """Reset the topological sort
        """
        self._topo_sort = []
        self._levels = {}

    def serialize_for(self, levels, min=1):
        """Serialize ops for multiple levels in the topological sort
        
        Args:
          levels: a list of strings of Python slicings
        """

        # build a list of indices
        xs = set()
        ys = [i for i in range(0, self.size)]
        for s in levels:
            if ":" in s:
                sl = slice(*map(lambda x: int(x.strip())
                                if x.strip()
                                else None, s.split(':')))
            else:
                idx = int(s)
                sl = slice(idx, idx+1, None)
            xs |= set(ys[sl])
        indices = sorted(list(xs))
        indices = [i for i in indices if i > min]

        prev_ops = set()
        prev_level = -1
        for i in indices:
            if (not prev_ops) and (prev_level + 1 == i):
                prev_ops = set()
            prev_ops = self._serialize_at(i, prev_ops)
            prev_level = i

        # rebuild the topo sort
        self.reset()
        self.build()

    def _serialize_at(self, level, prev_ops, rebuild=False):
        """Serialize ops at the same level in the topological sort
        """
        xs = self.get_ops(level)
        if xs is None:
            return set()
                
        # do not serialize levels including ops in the "/cond/" scope.
        cond_ops = {op for op in xs if "/cond/" in op.name}
        if len(cond_ops) > 0:
            return set()
        else:
            pass

        head_ops = {next(iter(xs))}
        tail_ops = xs - head_ops

        k_ops = head_ops
        if prev_ops:
            for op in k_ops:
                ut.add_control_inputs(
                    op,
                    prev_ops - set(op.control_inputs))

        for op in tail_ops:
            ut.add_control_inputs(
                op,
                k_ops - set(op.control_inputs))
            k_ops = {op}

        # rebuild the topo sort
        if rebuild:
            self.reset()
            self.build()
        else:
            pass

        return k_ops

    def get_level(self, op):
        """Return the level of an operation.

        Args:
          op: a `tf.Operation`.

        Return:
          An integer.
        """
        if op in self._levels:
            return self._levels[op]
        else:
            return None

    def get_ops(self, level):
        """Return a set of ops with the same level.

        Args:
          level: an integer.

        Return:
          A set of `tf.Operation`
        """
        if 0 <= level < len(self._topo_sort):
            return self._topo_sort[level]
        else:
            return None

    @property
    def size(self):
        """The number of levels in the topological level.
        """
        return len(self._topo_sort)
