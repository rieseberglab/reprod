"""
  Tools to generate build pipelines.
  The utilities here take a pipeline specification and convert
  them into a list of tasks to execute.
"""
from .graph import Cacheable, Transform, Target
from .utils import canonical_hash
from . import runtime
from .environment import ComputeEnv
from . import exc
from .constants import PLATFORM
from .containers import wrap_user_image

import json


class PipelineException(Exception):
    pass


class BuildNode(object):
    """graph of buildable things with dependencies"""
    __slots__ = ("data", "deps", "_uid", "_output_ready")

    def __init__(self, data):
        self.data = data  # Cacheable
        self.deps = []    # BuildNodes
        self._uid = None
        self._output_ready = None

    @property
    def uid(self):
        if not self._uid:
            if isinstance(self.data, Cacheable):
                self._uid = self.data.canonical_id
                if not isinstance(self._uid, str):
                    raise ValueError("Node %s computes a non string canonical id: %s", self.data, self._uid)
            else:
                self._uid = id(self.data)

        return self._uid

    def postorder(self, prune_fn=lambda x: False):
        """yield each node in DFS post order.
           use prune_fn() to prune out this node and its depencies

            - stop if prune_fn(self) == True
            - recurse into dependencies, yielding in turn
            - yield self
        """
        if prune_fn(self):
            return

        for dep in self.deps:
            for node in dep.postorder(prune_fn=prune_fn):
                yield node

        yield self

    @property
    def output_ready(self):
        """determines if the buildnode's output is readily available for consumption by downstream analyses"""
        if self._output_ready is None:
            if not isinstance(self.data, Target):
                raise TypeError("only valid on Targets")
            self._output_ready = self.data.exists()
        return self._output_ready

    def get_job_definition(self):
        XXX

    def execution_transfer_script(self):
        """
        Create a self-standing script that executes just the one node
        """

        # fixme -- json is an inefficient pickle.
        #          - slow
        #          - nodes will need to be dealiased again.
        #
        manifest_s = repr(json.dumps(self.data.manifest()))
        canonical_s = repr(json.dumps(self.data.canonical()))

        hooks = "\n".join(runtime.add_user_hook._hooks)

        return """#!/usr/bin/env python3
import bunnies.runtime
import bunnies.constants as C
from bunnies.unmarshall import unmarshall
import os.path
import json
import logging
log = logging.getLogger()

# USER HOOKS START
%(hooks)s
# USER HOOKS END

uid_s       = %(uid_s)s

canonical_s = %(canonical_s)s

manifest_s  = %(manifest_s)s

manifest_obj = json.loads(manifest_s)
canonical_obj = json.loads(canonical_s)

bunnies.setup_logging()

transform = unmarshall(manifest_obj)
log.info("%%s", json.dump(manifest_obj, indent=4))
output = transform.run()

result_path = os.path.join(transform.output_prefix(), C.TRANSFORM_RESULT_FILE)
bunnies.runtime.update_result(result_path, output=output, manifest=manifest_obj, canonical=canonical_obj)

""" % {
    'manifest_s': manifest_s,
    'canonical_s': canonical_s,
    'uid_s': repr(self.uid),
    'hooks': hooks
}


class BuildGraph(object):
    """Traverse the pipeline graph and obtain a graph of data dependencies"""

    def __init__(self):

        # cache of build nodes, by uid
        self.by_uid = {}

        # roots to build
        self.targets = []

    def _dealias(self, obj, path=None):
        """
           walk the graph, making sure there are no cycles,
           and dealias any Cacheable object along the way.
           The result is a BuildNode overlay on top of the data graph.
        """
        if path is None:
            path = set()

        # recurse in basic structures
        if isinstance(obj, list):
            return [self._dealias(o, path=path) for o in obj]
        if isinstance(obj, dict):
            return {k: self._dealias(v, path=path) for k, v in obj.items()}
        if isinstance(obj, tuple):
            return tuple([self._dealias(o, path=path) for o in obj])

        if not isinstance(obj, Cacheable):
            raise PipelineException("pipeline targets and their dependencies should be cacheable: %s" % (repr(obj)))

        # dealias object based on its uid

        if obj in path:
            # produce ordered proof of cycle
            raise PipelineException("Cycle in dependency graph detected: %s", path)

        path.add(obj)

        # fixme modularity -- need a "getDeps" interface
        if isinstance(obj, Transform):
            # recurse
            dealiased_deps = [self._dealias(obj.inputs[k].node, path=path) for k in obj.inputs]
        else:
            dealiased_deps = []

        path.remove(obj)

        build_node = BuildNode(obj)
        dealiased = self.by_uid.get(build_node.uid, None)

        if not dealiased:
            # first instance
            dealiased = build_node
            dealiased.deps = dealiased_deps
            self.by_uid[dealiased.uid] = dealiased

        return dealiased

    def add_targets(self, targets):
        if not isinstance(targets, (list, tuple)):
            targets = list([targets])

        all_targets = self.targets + self._dealias(targets)
        self.targets[:] = [x for x in set(all_targets)]

    def dependency_order(self):
        """generate the nodes of the graph in dependency order (if A depends on B, then B is yielded first).

           Nodes of the graph are visited once only.
        """
        seen = set()

        def _prune_visited(node):
            if node in seen:
                return True
            seen.add(node)
            return False

        for target in self.targets:
            if target in seen:
                continue
            for node in target.postorder(prune_fn=_prune_visited):
                yield node.data

    def build_order(self):
        """generate nodes in the graph that need to be built.
           if A depends on B, B is yielded first

           nodes that have already been built are skipped
        """
        seen = set()

        def _already_built(node):
            # visit only once
            if node in seen:
                return True
            seen.add(node)

            # prune if the result is already computed
            if node.output_ready:
                return True

            return False

        for target in self.targets:
            if target in seen:
                continue
            for node in target.postorder(prune_fn=_already_built):
                yield node

    def build(self, run_name):
        """run all the jobs that need to be run. managed resources created to build the chosen targets will be tagged with the
        given run_name. reusing the same name on a subsequent run will allow resources to be reused.
        """

        compute_env = ComputeEnv(envname)

        log.info("collecting job templates before scheduling")
        container_images = set()

        # FIXME repeatedly submit the current set of all jobs whose dependencies are satisfied
        for nodei, node in enumerate(self.build_order()):
            # check compatibility with compute_environment
            _ = node.task_template(compute_env)

        num_jobs = nodei + 1
        log.info("current number of jobs: %d", num_jobs)

        if num_jobs > 0:
            compute_env.create()
            compute_env.wait_ready()

        for job in self.build_order():
            job.schedule(compute_env)
            compute_env

        for job in self.build_order():
            XXX

def build_target(roots):

        """
    Construct a pipeline execution schedule from a graph of
    data dependencies.
    """
    if not isinstance(roots, list):
        roots = [roots]

    build_graph = BuildGraph()
    build_graph.add_targets(roots)

    return build_graph
