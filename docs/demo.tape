# Regenerate the README demo with:  vhs docs/demo.tape
#
# Records a real run against the demo cluster -- no scripted output. Bring the
# cluster up first:
#   kind create cluster --name triage-demo
#   kubectl apply -f demo/broken-pods.yaml

Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1100
Set Height 620
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set PlaybackSpeed 1.0

Hide
Type "cd /Users/ravirajput/Projects/AIOps-agent && clear"
Enter
Show

Sleep 800ms
Type "python agent.py 'Why does the memory-hog pod in demo keep restarting?'"
Sleep 600ms
Enter

# The real model call. Tool calls stream to stderr as they happen.
Sleep 75s

Sleep 3s
