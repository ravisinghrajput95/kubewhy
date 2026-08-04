# Regenerate the README demo with:  vhs docs/demo.tape
#
# Records a real run against the demo cluster -- the output is not scripted,
# and the pauses are the model actually thinking. Bring the cluster up first:
#   kind create cluster --name triage-demo
#   kubectl apply -f demo/broken-pods.yaml

Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1180
Set Height 560
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

# Setup off-camera. The venv must be active: a bare `python` picks up
# whatever is first on PATH, which is how the first recording captured a
# ModuleNotFoundError instead of a diagnosis.
Hide
Type "cd /Users/ravirajput/Projects/AIOps-agent"
Enter
Type "source .venv/bin/activate"
Enter
Type "clear"
Enter
Sleep 1s
Show

Sleep 1s
Type "python agent.py 'Why does typo-svc in demo have no endpoints?'"
Sleep 500ms
Enter

# Real inference. Tool calls stream to stderr as the model decides on them.
Sleep 60s
Sleep 4s
