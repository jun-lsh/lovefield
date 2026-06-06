AI / ML. Challenges involving a model: prompt injection, model extraction, evasion
/ adversarial examples, or an actual ML task to run. Our edge is a PC that won't
die running pytorch.

- Classify: is this attacking a deployed model (prompt-injection / jailbreak /
  guardrail bypass), stealing or inverting one (extraction / membership), fooling
  a classifier (adversarial perturbation), or just a task to train/run?
- Probe before theorizing: poke the model / load the provided weights locally and
  observe actual behavior.
- For attacks: craft the injection or the adversarial input; for tasks: run the
  training/inference locally and read off the answer.

Heavy compute (loading weights, training) is fine here - that's the lane's whole
point.
