# Manual Windows GUI tests

The GitLab `windows-gui-tests` job runs the complete discovered suite on a
native interactive Windows desktop with `NAGUMIX_GUI_TESTS=1`. It is manual and
initially allowed to fail so its runner and desktop behavior can be established
without weakening the Linux gate.

## Runner requirements

- Windows x64 GitLab shell executor using PowerShell or pwsh, tagged
  `windows-gui`
- latest supported Microsoft Visual C++ v14 Redistributable (x64)
- logged-in, unlocked interactive desktop for the complete job
- HTTPS access for the pinned uv tool, managed Python, and locked packages
- runner concurrency that prevents other jobs or people from sharing the test
  desktop during visible cases

The job uses project-local cache and environment directories. It preserves a
verbose log artifact for one week but does not build an executable or fabricate
a JUnit report. A green Linux job does not substitute for this native GUI job,
and an allowed Windows failure must still be inspected directly.

Runner assignment, permissions, cache availability, and interactive-session
behavior are host configuration, not properties transferred by Git history.
They must be re-established and verified for every new GitLab project.

