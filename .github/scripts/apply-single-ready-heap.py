from pathlib import Path

job_path = Path("src/compiler/job_queue/mod.rs")
job = job_path.read_text()

helper_start = job.index("fn desired_jobserver_tokens(")
helper_end = job.index(
    "/// This structure is backed by the `DependencyQueue` type and manages the\n"
    "/// actual compilation step of each package.",
    helper_start,
)
helper = """fn desired_jobserver_tokens(
    active: usize,
    ready: usize,
    jobs: u32,
    owns_jobserver: bool,
) -> usize {
    let needed = active.saturating_add(ready).saturating_sub(1);
    if owns_jobserver {
        needed.min(jobs.saturating_sub(1) as usize)
    } else {
        // An inherited jobserver may expose more capacity than Cargo's local
        // `jobs` setting. Preserve the previous one-request-per-ready-job
        // behavior instead of imposing a new local cap.
        needed
    }
}

#[cfg(test)]
mod scheduler_tests {
    use super::desired_jobserver_tokens;

    #[test]
    fn bounds_owned_jobserver_requests_by_work_and_parallelism() {
        assert_eq!(desired_jobserver_tokens(0, 0, 8, true), 0);
        assert_eq!(desired_jobserver_tokens(0, 1, 8, true), 0);
        assert_eq!(desired_jobserver_tokens(0, 10, 4, true), 3);
        assert_eq!(desired_jobserver_tokens(2, 0, 4, true), 1);
        assert_eq!(desired_jobserver_tokens(2, 10, 1, true), 0);
    }

    #[test]
    fn does_not_cap_an_inherited_jobserver() {
        assert_eq!(desired_jobserver_tokens(0, 10, 4, false), 9);
        assert_eq!(desired_jobserver_tokens(2, 10, 4, false), 11);
    }
}

"""
job = job[:helper_start] + helper + job[helper_end:]

old_call = """        let desired_tokens = desired_jobserver_tokens(
            self.active.len(),
            self.queue.ready_len(),
            build_runner.bcx.jobs(),
        );
"""
new_call = """        let desired_tokens = desired_jobserver_tokens(
            self.active.len(),
            self.queue.ready_len(),
            build_runner.bcx.jobs(),
            build_runner.bcx.gctx.jobserver_from_env().is_none(),
        );
"""
assert old_call in job
job = job.replace(old_call, new_call, 1)
job = job.replace(
    "        // Keep enough token requests in flight to saturate the configured\n"
    "        // parallelism. Ready jobs stay in the dependency queue until capacity\n"
    "        // exists, avoiding a second priority queue and one request per unit.\n",
    "        // Keep enough token requests in flight to saturate a Cargo-owned\n"
    "        // jobserver. Ready jobs stay in the dependency queue until capacity\n"
    "        // exists, avoiding a second priority queue and excess requests.\n",
    1,
)
job_path.write_text(job)

dependency_path = Path("src/util/dependency_queue.rs")
dependency = dependency_path.read_text()
dependency = dependency.replace(
    ".then_with(|| self.order.cmp(&other.order))",
    ".then_with(|| other.order.cmp(&self.order))",
    1,
)
dependency = dependency.replace(
    "    /// Nodes with no remaining dependencies, ordered by priority and then by\n"
    "    /// the same map order used by the previous scan-based implementation.\n",
    "    /// Nodes with no remaining dependencies, ordered by priority and then by\n"
    "    /// the dispatch order of the previous scan-plus-pending-queue scheduler.\n",
    1,
)
old_comment = """        // `Iterator::max_by_key`, used by the previous dequeue implementation,
        // selected the last equal-priority item in map iteration order. Retain
        // that order as a tie-breaker so replacing the scan does not change the
        // schedule for equal priorities.
"""
new_comment = """        // The previous scheduler scanned ready nodes with `max_by_key`, inserted
        // the resulting sequence into a sorted pending queue, and popped from
        // its end. Those two reversals dispatched equal-priority nodes in map
        // iteration order. Retain that final dispatch order as the tie-breaker.
"""
assert old_comment in dependency
dependency = dependency.replace(old_comment, new_comment, 1)

tests_start = dependency.index("    #[test]\n    fn preserves_equal_priority_scan_order()")
new_tests = """    #[test]
    fn preserves_equal_priority_dispatch_order() {
        let mut q: DependencyQueue<i32, (), ()> = DependencyQueue::new();

        for node in 0..16 {
            q.queue(node, (), std::iter::empty::<(i32, ())>(), 1);
        }

        let expected = q.dep_map.keys().copied().collect::<Vec<_>>();
        q.queue_finished();

        let actual =
            std::iter::from_fn(|| q.dequeue().map(|(node, (), _)| node)).collect::<Vec<_>>();
        assert_eq!(actual, expected);
    }

    #[test]
    fn heap_matches_previous_dispatch_across_dynamic_frontiers() {
        for seed in 0..16 {
            let mut heap: DependencyQueue<usize, (), ()> = DependencyQueue::new();
            let mut scan: DependencyQueue<usize, (), ()> = DependencyQueue::new();

            for node in 0..64 {
                let dependencies = (0..node)
                    .filter(|dependency| (node * 37 + dependency * 17 + seed * 13) % 11 < 2)
                    .map(|dependency| (dependency, ()))
                    .collect::<Vec<_>>();
                let cost = (node + seed) % 5 + 1;
                heap.queue(node, (), dependencies.clone(), cost);
                scan.queue(node, (), dependencies, cost);
            }
            heap.queue_finished();
            scan.queue_finished();

            loop {
                let expected = drain_ready_by_previous_scheduler(&mut scan);
                let actual = std::iter::from_fn(|| heap.dequeue()).collect::<Vec<_>>();
                assert_eq!(actual, expected, "seed {seed}");

                if actual.is_empty() {
                    break;
                }
                for (node, (), _) in actual {
                    heap.finish(&node, &());
                    scan.finish(&node, &());
                }
            }

            assert!(heap.is_empty(), "seed {seed}");
            assert!(scan.is_empty(), "seed {seed}");
        }
    }

    fn drain_ready_by_previous_scheduler(
        queue: &mut DependencyQueue<usize, (), ()>,
    ) -> Vec<(usize, (), usize)> {
        let mut pending = Vec::new();
        while let Some((key, value, priority)) = dequeue_by_scan(queue) {
            let index = pending.partition_point(|&(_, _, queued_priority)| {
                queued_priority <= priority
            });
            pending.insert(index, (key, value, priority));
        }
        std::iter::from_fn(|| pending.pop()).collect()
    }

    fn dequeue_by_scan(queue: &mut DependencyQueue<usize, (), ()>) -> Option<(usize, (), usize)> {
        let (key, priority) = queue
            .dep_map
            .iter()
            .filter(|(_, (dependencies, _))| dependencies.is_empty())
            .map(|(key, _)| (*key, queue.priority[key].0))
            .max_by_key(|(_, priority)| *priority)?;
        let (_, value) = queue.dep_map.remove(&key).unwrap();
        Some((key, value, priority))
    }
}
"""
dependency = dependency[:tests_start] + new_tests
dependency_path.write_text(dependency)
