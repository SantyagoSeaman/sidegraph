# Finding the Python of an installed plugin

An installed plugin has no `.venv` of its own. Its MCP server runs from a pinned environment that
the launcher keeps in the uv cache, and the Python of that environment is the already-existing
interpreter for the collector. Identify it from the running server; do not search the cache, do
not guess a Python from `PATH`, and never create or repair an environment.

1. List the running processes with their full command lines: `ps -eo pid=,args=` on macOS and
   Linux, or, in PowerShell on Windows,
   `Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine | Format-List`
   (the default table view cuts the command line off).
2. Keep the lines whose command starts with an interpreter and runs `sidegraph-mcp`. A server's
   line reads `<env>/bin/python <env>/bin/sidegraph-mcp --repo-root` (Windows:
   `<env>\Scripts\python.exe <env>\Scripts\sidegraph-mcp.exe ...`). The interpreter is the text
   from the start of the command up to the first `/bin/python` that is followed by a space
   (Windows: the first `\Scripts\python.exe` followed by a space), never "the first word": a
   user, home or cache directory name can hold a space. Ignore a line that merely mentions
   `sidegraph-mcp` (a shell running this very command, for example) and the `uv` or `uvx` process
   that started the server.
3. Read each kept process's working directory: `lsof -a -p <pid> -d cwd -Fn` on macOS,
   `readlink /proc/<pid>/cwd` on Linux. Both print the real path, so resolve the project root the
   same way (`git rev-parse --show-toplevel` prints a real path; resolve a symlinked `$PWD` with
   `cd -P`) and compare whole path components, ignoring a trailing slash: `/work/app` contains
   `/work/app/src` but not `/work/app2`.
4. Keep the processes whose working directory is the project root, or a directory inside it that
   belongs to the same Git repository (`git -C <directory> rev-parse --show-toplevel` equals the
   project's top level): the server of a nested repository belongs to that repository. Then
   reduce them to distinct interpreters.
   - One distinct interpreter is the one to use.
   - Several distinct interpreters mean sessions from different plugin versions: a session that
     started before a plugin update keeps its older pinned environment. Read each one's version
     with `"<interpreter>" -I -B -c "import importlib.metadata as m; print(m.version('sidegraph'))"`
     (PowerShell: the same command after `&`). Prefer the interpreters whose version equals the
     `version` in `.claude-plugin/plugin.json` of the plugin that holds this skill (two
     directories above the skill directory). That is the plugin version this session loaded, so
     the match is evidence of the session's own environment, not proof. If several share that
     version, any of them will do. If none matches, no interpreter is identified: say so and name
     the versions found. Whichever you choose, say in the report that it was chosen among
     several environments and by what.

Where no working directory can be read (Windows), skip step 3 and apply step 4 to every
`sidegraph-mcp` process. With no such process, or with several interpreters that the version
check cannot separate, no interpreter is identified: say so in the report and stop, as the skill
describes.

Several sessions of one plugin version, and one pinned environment shared by several projects,
give the same interpreter. The collector reports the version of the package it runs as the
collector runtime; a version that differs from the plugin's is a finding, not a reason to switch
interpreters.
