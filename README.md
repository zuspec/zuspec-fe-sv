# Zuspec SystemVerilog Frontend

A SystemVerilog to Zuspec IR translator focusing on 2-state types and class-based designs for UVM verification environments.

## Status: Phase 4 Complete ✅

**All Core Phases Implemented:**
- ✅ Phase 1: Foundation (parser, types, error reporting)
- ✅ Phase 2: Classes and structures
- ✅ Phase 3: Expressions and statements
- ✅ Phase 4: Functions and methods

**Test Coverage:** 59/59 passing tests (100%)

## Installation

### Quick Install

```bash
pip install -e .
```

### With Development Tools

```bash
pip install -e ".[dev]"
```

See [Installation Guide](docs/installation.md) for detailed instructions.

## Features

### Fully Supported
- ✅ **2-State Types**: bit, byte, shortint, int, longint with width specifications
- ✅ **Class Definitions**: Basic class structure with fields and inheritance
- ✅ **Expressions**: Binary ops, unary ops, comparisons, literals, references, member access, subscripts, function calls
- ✅ **Statements**: Assignments, returns, if/else, while loops, for loops, break/continue
- ✅ **Strict Error Reporting**: Clear messages for unsupported constructs
- ✅ **Macro Expansion**: Automatic via slang preprocessor

### Explicitly Not Supported (with errors)
- ❌ **4-State Types**: logic, reg, integer (generates helpful error messages)
- ❌ **4-State Operators**: ===, !==, ==?, !=?
- ❌ **Modules**: Deferred to future implementation
- ❌ **Interfaces**: Deferred to future implementation

## Installation

Requires:
- Python 3.8+
- pyslang (SystemVerilog parser)
- zuspec-dataclasses (Zuspec IR definitions)

## Usage

```python
from zuspec.fe.sv.mapper import SVMapper

# Create mapper
mapper = SVMapper()

# Map SystemVerilog code
code = """
class transaction;
    bit [31:0] addr;
    bit [7:0] data;
    bit write;
    
    function bit is_write();
        return write;
    endfunction
    
    function int get_addr();
        if (write)
            return addr;
        else
            return 0;
    endfunction
endclass
"""

# Perform mapping
if mapper.map_text(code):
    # Success - get mapped classes
    for cls in mapper.get_classes():
        print(f"Class: {cls.name}")
        for field in cls.fields:
            print(f"  Field: {field.name}: {field.datatype.bits} bits")
else:
    # Error - print report
    print(mapper.get_error_report())
```

## Running Tests

```bash
cd /path/to/zuspec-fe-sv
python3 -m pytest tests/unit/ -v
```

All 59 tests should pass.

## Project Structure

```
zuspec-fe-sv/
├── src/zuspec/fe/sv/          # Source code
│   ├── config.py              # Configuration system
│   ├── error.py               # Error reporting
│   ├── parser.py              # SystemVerilog parser wrapper
│   ├── type_mapper.py         # Type mapping (2-state)
│   ├── class_mapper.py        # Class structure mapping
│   ├── expr_mapper.py         # Expression mapping
│   ├── stmt_mapper.py         # Statement mapping
│   ├── function_mapper.py     # Function/task mapping
│   └── mapper.py              # Main orchestrator
├── tests/unit/                # Unit tests
├── docs/                      # Documentation
├── pyproject.toml             # Project configuration
└── README.md                  # This file
```

## Dependencies

- **Python**: 3.8 or higher
- **pyslang**: >=3.0 (SystemVerilog parser)
- **zuspec-dataclasses**: Zuspec IR definitions

## Architecture

The implementation follows a modular, layered design:

```
┌─────────────────────────────────────────┐
│           SVMapper (Main API)           │
└─────────────────────────────────────────┘
                    │
        ┌───────────┴───────────┐
        ↓                       ↓
┌──────────────┐       ┌──────────────┐
│   SVParser   │       │ ErrorReporter│
└──────────────┘       └──────────────┘
        │
        ↓
┌──────────────┐
│  TypeMapper  │ ──→ 2-state types only
└──────────────┘
        │
        ↓
┌──────────────┐
│ ClassMapper  │ ──→ Class structure
└──────────────┘
        │
        ↓
┌──────────────┐
│  ExprMapper  │ ──→ Expressions
└──────────────┘
        │
        ↓
┌──────────────┐
│  StmtMapper  │ ──→ Statements
└──────────────┘
```

**Modules:**
- **config.py**: Configuration settings
- **error.py**: Error collection and reporting
- **parser.py**: SystemVerilog parser wrapper (using pyslang)
- **type_mapper.py**: Type mapping (2-state only)
- **class_mapper.py**: Class structure mapping
- **expr_mapper.py**: Expression mapping
- **stmt_mapper.py**: Statement mapping
- **mapper.py**: Main orchestrator

## Supported Constructs

### Types
- `bit`, `bit[N:0]` - 2-state unsigned
- `byte` - 8-bit signed
- `shortint` - 16-bit signed
- `int` - 32-bit signed
- `longint` - 64-bit signed

### Expressions
- Arithmetic: `+`, `-`, `*`, `/`, `%`
- Bitwise: `&`, `|`, `^`, `<<`, `>>`
- Logical: `&&`, `||`, `!`
- Comparison: `==`, `!=`, `<`, `<=`, `>`, `>=`
- Unary: `!`, `~`, `+`, `-`
- Member access: `obj.field`
- Subscript: `arr[i]`
- Function calls: `func(a, b)`

### Statements
- Assignment: `x = y;`
- Return: `return expr;`
- Conditional: `if (cond) stmt; else stmt;`
- While loop: `while (cond) stmt;`
- For loop: `for (init; cond; incr) stmt;`
- Break/Continue: `break;`, `continue;`

### Classes
- Class definitions
- Fields (properties)
- Inheritance (detected, resolution deferred)

## Design Principles

1. **2-State Only**: Only 2-state types are supported. 4-state types (logic, reg, integer) generate clear errors.

2. **Classes Only**: Focus on class-based designs. Modules and interfaces deferred.

3. **Strict Validation**: Unsupported features generate errors immediately with helpful suggestions.

4. **Incremental Implementation**: Foundation established for future enhancements.

## Documentation

- [Installation Guide](docs/installation.md) - Detailed setup and usage
- [Implementation Plan](docs/implementation_plan.md) - Original roadmap (complete)
- [Phase 3 Completion](docs/phase3_completion.md) - Expression and statement mapping
- [Phase 4 Completion](docs/phase4_completion.md) - Function and method mapping
- [Final Summary](docs/final_summary.md) - Complete project overview

## Future Enhancements

- Parameterized class support for UVM templates
- TLM port/export special handling
- Local variable declarations in functions
- String type full support
- Real UVM library testing

## License

Apache License 2.0 - See LICENSE file.
