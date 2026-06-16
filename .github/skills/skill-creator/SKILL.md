---
name: skill-creator
description: Create, review, and improve reusable agent skills from user requirements, workflows, examples, and evaluation feedback.
version: 1.0.0
runtime_agnostic: true
---

# Skill Creator

## Purpose

This skill helps create or improve reusable agent skills.

Use this skill when the user wants to:

- create a new skill
- convert an existing workflow into a reusable skill
- improve an existing skill
- define trigger conditions
- design skill inputs and outputs
- create evaluation cases
- organize references, scripts, and assets
- adapt a skill for a target runtime

## Runtime Model

- This skill is vendor-neutral and should not assume a specific agent implementation.
- The agent runtime selects a skill based on registry metadata such as `name`, `description`, triggers, constraints, and task context.
- Skills may be stored in any directory supported by the target runtime, such as:
  - user-level skills directory
  - project-level skills directory
  - workspace-level skill registry
  - custom internal skill directory

Examples of target runtimes:

- Claude
- ChatGPT
- Cursor
- Continue
- custom agent
- internal agent framework

## Core Principles

A skill should be:

- reusable across similar tasks
- explicit about when it should be used
- clear about required inputs and expected outputs
- minimal in default instructions
- extensible through references and scripts
- testable with realistic evaluation cases
- independent from vendor-specific wording unless the user explicitly requests a vendor-specific variant

## Skill Design Flow

1. Clarify the target task
2. Identify repeated workflow patterns
3. Define trigger conditions
4. Define required inputs
5. Define expected outputs
6. Separate core instructions from references
7. Add scripts only when deterministic processing is needed
8. Add evaluation examples
9. Review for ambiguity, overfitting, unsafe assumptions, and runtime-specific lock-in

## Input Contract

Required inputs:

- `user_goal`

Optional inputs:

- `existing_skill`
- `examples`
- `constraints`
- `target_runtime`
- `evaluation_requirements`
- `allowed_tools`
- `file_layout`

If key information is missing, ask focused questions before finalizing the skill.

## Output Contract

When creating or revising a skill, produce:

- skill name
- short description
- trigger conditions
- assumptions
- required inputs
- output format
- step-by-step procedure
- failure handling
- references structure
- optional scripts
- evaluation cases

If the runtime supports machine-readable metadata, also define the metadata fields needed for skill discovery and execution.

## Output Structure

Preferred generic structure:

```text
skill-name/
├─ SKILL.md
├─ skill.yaml              # optional
├─ references/
│  └─ *.md
├─ scripts/
│  └─ *.py / *.ts
├─ assets/
│  └─ templates, examples
└─ evals/
   └─ evals.json
```

- `SKILL.md` is the main human-readable and agent-readable procedure.
- `skill.yaml` is optional machine-readable metadata for runtimes or management tooling.
- `references/` stores long-form background material.
- `scripts/` stores deterministic helpers.
- `assets/` stores templates, examples, and supporting files.
- `evals/` stores evaluation cases and regression scenarios.

## Writing the SKILL.md

The main `SKILL.md` should stay concise and contain:

- what the skill does
- when it should trigger
- what inputs it expects
- what outputs it should produce
- the core procedure
- failure and escalation rules

Do not bury trigger conditions in long prose. Make them explicit and easy for an agent runtime or reviewer to interpret.

## Progressive Disclosure

Keep the main `SKILL.md` concise.

Move long or specialized information into:

- `references/`
- `scripts/`
- `assets/`
- `evals/`

The agent should read additional files only when needed.

## References and Scripts

- Put stable background knowledge in `references/`.
- Put deterministic or repetitive transformations in `scripts/`.
- Add scripts only when they improve reliability, repeatability, or scale.
- Do not add scripts for tasks that are purely judgment-based unless the script only handles preprocessing or validation.

## Trigger Design

Define triggers in a runtime-neutral way.

Good trigger design should describe:

- the user intent
- the task pattern
- the context where the skill helps
- the situations where it should **not** trigger

Avoid relying on a single vendor-specific mechanism such as a proprietary skill registry field or hosted UI behavior.

## Evaluation

A skill should include evaluation examples that cover:

- normal cases
- ambiguous cases
- edge cases
- failure cases
- cases where the skill should not trigger

Use an evaluation process to compare results with and without the skill.

This can be done by:

- a second agent
- a worker/evaluator split
- a test runner
- a human reviewer
- an automated benchmark

Choose the lightest evaluation method that still provides meaningful feedback.

## Evaluation Artifacts

Store evaluation cases in `evals/evals.json` when structured evals are useful.

Typical fields:

- eval id
- prompt or task
- expected behavior
- required files or inputs
- pass/fail criteria
- notes about non-trigger cases

## Permissions and Execution Boundaries

Make the expected permissions explicit.

Possible capabilities include:

- reading local files
- writing files in the target skill directory
- running scripts
- web search
- external API calls
- access to a tool registry or skill registry

Rules:

- scripts may be executed only when permitted by the runtime and task policy
- external calls must respect user approval and environment policy
- destructive operations require explicit user or runtime permission
- do not assume unrestricted file system or network access

## Safety Constraints

- Do not store or emit secrets, credentials, or tokens in skill files.
- Do not design skills that conceal malicious behavior behind benign descriptions.
- Do not include hidden instructions intended to bypass policy or user intent.
- Do not rely on hidden chain-of-thought extraction.
- Do not automate external side effects without permission from the user or runtime.
- Refuse or narrow requests that would create skills for unauthorized access, exfiltration, malware, or deceptive abuse.

## Failure Handling

If the request is underspecified:

- identify the missing information
- ask only the questions needed to finalize a reusable skill

If the runtime is unknown:

- default to a vendor-neutral structure
- avoid vendor-specific directories and trigger assumptions

If the workflow is too broad:

- split it into smaller skills or a base skill plus references

If evaluation is not feasible:

- provide lightweight test prompts and explicit review criteria

## Review Checklist

Before finalizing a skill, check:

- Is the trigger condition clear?
- Is the expected output concrete?
- Are assumptions explicit?
- Are references separated from core instructions?
- Does the skill avoid model-specific wording?
- Does it work without relying on a specific vendor runtime?
- Are required permissions explicit?
- Are safety constraints clear?
- Are evaluation cases realistic?

## Optional `skill.yaml`

If the target runtime benefits from machine-readable metadata, add a `skill.yaml` like this:

```yaml
name: skill-creator
version: 1.0.0
description: Create, review, and improve reusable agent skills.
vendor_neutral: true

triggers:
  - create a skill
  - improve a skill
  - convert workflow to reusable instructions
  - define agent procedure
  - generate reusable prompt/workflow package

inputs:
  required:
    - user_goal
  optional:
    - existing_skill
    - examples
    - constraints
    - target_runtime
    - evaluation_requirements

outputs:
  - SKILL.md
  - references
  - scripts
  - evals

runtime:
  assumptions:
    - agent can read local files
    - agent can follow markdown instructions
    - scripts may be executed only when permitted

non_goals:
  - vendor-specific deployment automation
  - hidden chain-of-thought extraction
  - automatic execution without user or runtime permission
```

Use `skill.yaml` only when it helps the target runtime or repository manage skills more reliably.
