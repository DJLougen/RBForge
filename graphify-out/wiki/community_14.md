# Community 14: SandboxExecutor

**Members:** 7

## Nodes

- **_docker_is_ready()** (`src_ornstein_rbforge_sandbox_py_docker_is_ready`, Function, degree: 2)
- **_run()** (`src_ornstein_rbforge_sandbox_py_run`, Function, degree: 3)
- **SandboxExecutor** (`src_ornstein_rbforge_sandbox_py_sandboxexecutor`, Class, degree: 5)
- **.__init__()** (`src_ornstein_rbforge_sandbox_py_sandboxexecutor_init`, Method, degree: 1)
- **._run_docker()** (`src_ornstein_rbforge_sandbox_py_sandboxexecutor_run_docker`, Method, degree: 3)
- **._run_local()** (`src_ornstein_rbforge_sandbox_py_sandboxexecutor_run_local`, Method, degree: 3)
- **.validate()** (`src_ornstein_rbforge_sandbox_py_sandboxexecutor_validate`, Method, degree: 6)

## Relationships

- src_ornstein_rbforge_sandbox_py_sandboxexecutor → src_ornstein_rbforge_sandbox_py_sandboxexecutor_init (defines)
- src_ornstein_rbforge_sandbox_py_sandboxexecutor → src_ornstein_rbforge_sandbox_py_sandboxexecutor_validate (defines)
- src_ornstein_rbforge_sandbox_py_sandboxexecutor → src_ornstein_rbforge_sandbox_py_sandboxexecutor_run_docker (defines)
- src_ornstein_rbforge_sandbox_py_sandboxexecutor → src_ornstein_rbforge_sandbox_py_sandboxexecutor_run_local (defines)
- src_ornstein_rbforge_sandbox_py_sandboxexecutor_validate → src_ornstein_rbforge_sandbox_py_sandboxexecutor_run_local (calls)
- src_ornstein_rbforge_sandbox_py_sandboxexecutor_validate → src_ornstein_rbforge_sandbox_py_sandboxexecutor_run_docker (calls)
- src_ornstein_rbforge_sandbox_py_sandboxexecutor_validate → src_ornstein_rbforge_sandbox_py_docker_is_ready (calls)
- src_ornstein_rbforge_sandbox_py_sandboxexecutor_run_docker → src_ornstein_rbforge_sandbox_py_run (calls)
- src_ornstein_rbforge_sandbox_py_sandboxexecutor_run_local → src_ornstein_rbforge_sandbox_py_run (calls)

