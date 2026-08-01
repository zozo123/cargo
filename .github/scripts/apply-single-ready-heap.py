from pathlib import Path

job_path = Path("src/compiler/job_queue/mod.rs")
text = job_path.read_text()

imports = "use std::cmp::Ordering;\nuse std::collections::BinaryHeap;\n"
assert imports in text
text = text.replace(imports, "", 1)

pending_start = text.index("/// A pending job ordered by scheduling priority.")
drain_docs = text.index(
    "/// This structure is backed by the `DependencyQueue` type and manages the\n"
    "/// actual compilation step of each package.",
    pending_start,
)
helper = """fn desired_jobserver_tokens(active: usize, ready: usize, jobs: u32) -> usize {
    active
        .saturating_add(ready)
        .saturating_sub(1)
        .min(jobs.saturating_sub(1) as usize)
}

#[cfg(test)]
mod scheduler_tests {
    use super::desired_jobserver_tokens;

    #[test]
    fn bounds_jobserver_requests_by_work_and_parallelism() {
        assert_eq!(desired_jobserver_tokens(0, 0, 8), 0);
        assert_eq!(desired_jobserver_tokens(0, 1, 8), 0);
        assert_eq!(desired_jobserver_tokens(0, 10, 4), 3);
        assert_eq!(desired_jobserver_tokens(2, 0, 4), 1);
        assert_eq!(desired_jobserver_tokens(2, 10, 1), 0);
    }
}

"""
text = text[:pending_start] + helper + text[drain_docs:]

old_fields = """    /// The list of jobs that we have not yet started executing, but have
    /// retrieved from the `queue`. We eagerly pull jobs off the main queue to
    /// allow us to request jobserver tokens pretty early.
    pending_queue: BinaryHeap<Pending<(Unit, Job)>>,
    next_pending_order: usize,
"""
new_fields = """    /// Jobserver token requests that have not completed yet.
    pending_token_requests: usize,
"""
assert old_fields in text
text = text.replace(old_fields, new_fields, 1)

old_init = """            tokens: Vec::new(),
            pending_queue: BinaryHeap::new(),
            next_pending_order: 0,
            print: DiagnosticPrinter::new(
"""
new_init = """            tokens: Vec::new(),
            pending_token_requests: 0,
            print: DiagnosticPrinter::new(
"""
assert old_init in text
text = text.replace(old_init, new_init, 1)

function_start = text.index("    fn spawn_work_if_possible<'s>(")
function_end = text.index("    fn has_extra_tokens(&self) -> bool {", function_start)
new_function = """    fn spawn_work_if_possible<'s>(
        &mut self,
        build_runner: &mut BuildRunner<'_, '_>,
        jobserver_helper: &HelperThread,
        scope: &'s Scope<'s, '_>,
    ) -> CargoResult<()> {
        // Keep enough token requests in flight to saturate the configured
        // parallelism. Ready jobs stay in the dependency queue until capacity
        // exists, avoiding a second priority queue and one request per unit.
        let desired_tokens = desired_jobserver_tokens(
            self.active.len(),
            self.queue.ready_len(),
            build_runner.bcx.jobs(),
        );
        let requested_tokens = self.tokens.len() + self.pending_token_requests;
        for _ in requested_tokens..desired_tokens {
            jobserver_helper.request_token();
            self.pending_token_requests += 1;
        }

        while self.has_extra_tokens() {
            let Some((unit, job, _priority)) = self.queue.dequeue() else {
                break;
            };
            *self.counts.get_mut(&unit.pkg.package_id()).unwrap() -= 1;
            // Print out some nice progress information.
            // NOTE: An error here will drop the job without starting it.
            // That should be OK, since we want to exit as soon as
            // possible during an error.
            self.note_working_on(
                build_runner.bcx.gctx,
                build_runner.bcx.ws.root(),
                &unit,
                job.freshness(),
            )?;
            self.run(&unit, job, build_runner, scope);
        }

        Ok(())
    }

"""
text = text[:function_start] + new_function + text[function_end:]

old_token = """            Message::Token(acquired_token) => {
                let token = acquired_token.context("failed to acquire jobserver token")?;
                self.tokens.push(token);
            }
"""
new_token = """            Message::Token(acquired_token) => {
                self.pending_token_requests = self
                    .pending_token_requests
                    .checked_sub(1)
                    .expect("received a jobserver token without requesting one");
                let token = acquired_token.context("failed to acquire jobserver token")?;
                self.tokens.push(token);
            }
"""
assert old_token in text
text = text.replace(old_token, new_token, 1)

old_done = "        } else if self.queue.is_empty() && self.pending_queue.is_empty() {\n"
assert old_done in text
text = text.replace(old_done, "        } else if self.queue.is_empty() {\n", 1)

old_loop_docs = """        // loop starts out by scheduling as much work as possible (up to the
        // maximum number of parallel jobs we have tokens for). A local queue
        // is maintained separately from the main dependency queue as one
        // dequeue may actually dequeue quite a bit of work (e.g., 10 binaries
        // in one package).
"""
new_loop_docs = """        // loop starts out by requesting enough jobserver tokens for the ready
        // frontier and scheduling as much work as possible directly from the
        // dependency queue.
"""
assert old_loop_docs in text
text = text.replace(old_loop_docs, new_loop_docs, 1)
job_path.write_text(text)

dependency_path = Path("src/util/dependency_queue.rs")
dependency = dependency_path.read_text()
old_len = """    pub fn len(&self) -> usize {
        self.dep_map.len()
    }

    /// Indicate that something has finished.
"""
new_len = """    pub fn len(&self) -> usize {
        self.dep_map.len()
    }

    /// Returns the number of packages that can be dequeued immediately.
    pub(crate) fn ready_len(&self) -> usize {
        self.ready.len()
    }

    /// Indicate that something has finished.
"""
assert old_len in dependency
dependency_path.write_text(dependency.replace(old_len, new_len, 1))
