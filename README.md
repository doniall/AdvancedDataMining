# Learning Python Through Projects

This repo is a beginner path into Python. No prior programming experience assumed.
Instead of reading through syntax reference material, you build a small project
per concept, then use it as the foundation for the next one.

## Setup

You need Python 3.10+ installed. Check with:

```
python3 --version
```

Run any script with:

```
python3 path/to/starter.py
```

No third-party packages are required for this curriculum — everything uses
Python's standard library.

## How each project works

Every project folder has:

- `README.md` — what you're building, the concepts it teaches, and step-by-step
  instructions.
- `starter.py` — a skeleton with `TODO` comments. This is what you edit.
- `solution.py` — a complete, working version. Don't open it until you've had a
  real attempt, or you're stuck and want to compare approaches.

Work through the projects in order — each one assumes you're comfortable with
the concepts from the ones before it.

## Projects

| # | Project | Concepts introduced |
|---|---------|---------------------|
| 01 | [Mad Libs](projects/01_mad_libs/) | `print`, `input`, variables, f-strings |
| 02 | [Number Guessing Game](projects/02_number_guessing_game/) | `if`/`elif`/`else`, `while` loops, the `random` module |
| 03 | [To-Do List](projects/03_todo_list/) | lists, `for` loops, reading/writing text files |
| 04 | [Calculator](projects/04_calculator_functions/) | functions, arguments, return values, error handling |
| 05 | [Quiz Game](projects/05_quiz_game/) | dictionaries, JSON files, combining functions |
| 06 | [Contact Book](projects/06_contact_book_oop/) | classes, objects, methods, persisting objects to disk |
| 07 | [CSV Data Explorer](projects/07_csv_data_explorer/) | reading CSV data, aggregating, basic stats — a bridge toward data mining |

## Getting stuck

Being stuck is normal and part of learning. Before checking `solution.py`:

1. Re-read the error message. Python's errors usually tell you the exact line
   and reason.
2. Add `print()` statements to see what a variable actually holds at that point.
3. Break the problem into a smaller piece and test that piece alone.

If you've genuinely tried and want to compare against a working version, open
`solution.py` — but type it out yourself rather than copy-pasting, you retain
far more that way.
