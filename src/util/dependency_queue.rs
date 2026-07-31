//! A graph-like structure used to represent a set of dependencies and in what
//! order they should be built.
//!
//! This structure is used to store the dependency graph and dynamically update
//! it to figure out when a dependency should be built.
//!
//! Dependencies in this queue are represented as a (node, edge) pair. This is
//! used to model nodes which produce multiple outputs at different times but
//! some nodes may only require one