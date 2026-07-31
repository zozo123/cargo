use cargo::util::DependencyQueue;
use criterion::{BenchmarkId, Criterion, criterion_group, criterion_main};

fn drain_independent_nodes(c: &mut Criterion) {
    let mut group = c.benchmark_group("dependency_queue/drain_independent");

    for nodes in [100usize, 1_000, 10_000] {
        group.bench_with_input(BenchmarkId::from_parameter(nodes), &nodes, |b, &nodes| {
            b.iter(|| {
                let mut queue = DependencyQueue::new();
                for node in 0..nodes {
                    queue.queue(node, (), std::iter::empty::<(usize, ())>(), 1);
                }
                queue.queue_finished();

                let mut dequeued = 0;
                while queue.dequeue().is_some() {
                    dequeued += 1;
                }
                assert_eq!(dequeued, nodes);
            });
        });
    }

    group.finish();
}

criterion_group!(benches, drain_independent_nodes);
criterion_main!(benches);
