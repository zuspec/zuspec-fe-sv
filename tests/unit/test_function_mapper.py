"""Unit tests for function mapper."""
import pytest
from zuspec.fe.sv.mapper import SVMapper
from zuspec.dataclasses.ir.data_type import Function


def test_map_simple_function():
    """Test mapping a simple function."""
    mapper = SVMapper()
    
    code = """
    class test;
        function int add(int a, int b);
            return a + b;
        endfunction
    endclass
    """
    
    result = mapper.map_text(code)
    
    assert result is True
    assert not mapper.has_errors()
    
    classes = mapper.get_classes()
    assert len(classes) == 1
    
    test_class = classes[0]
    assert len(test_class.functions) > 0
    
    # Find the add function
    add_func = None
    for func in test_class.functions:
        if func.name == 'add':
            add_func = func
            break
    
    assert add_func is not None
    assert add_func.name == 'add'
    assert add_func.is_async is False
    assert add_func.returns is not None  # int return type
    assert add_func.args is not None
    assert len(add_func.args.args) == 2  # a and b parameters


def test_map_void_function():
    """Test mapping a void function."""
    mapper = SVMapper()
    
    code = """
    class test;
        int x;
        function void set_x(int val);
            x = val;
        endfunction
    endclass
    """
    
    result = mapper.map_text(code)
    
    assert result is True
    
    classes = mapper.get_classes()
    assert len(classes) == 1
    
    test_class = classes[0]
    
    # Find set_x function
    set_x_func = None
    for func in test_class.functions:
        if func.name == 'set_x':
            set_x_func = func
            break
    
    assert set_x_func is not None
    assert set_x_func.returns is None  # void
    assert len(set_x_func.args.args) == 1


def test_map_task():
    """Test mapping a task (async function)."""
    mapper = SVMapper()
    
    code = """
    class test;
        task run();
            // Task body
        endtask
    endclass
    """
    
    result = mapper.map_text(code)
    
    assert result is True
    
    classes = mapper.get_classes()
    assert len(classes) == 1
    
    test_class = classes[0]
    
    # Find run task
    run_task = None
    for func in test_class.functions:
        if func.name == 'run':
            run_task = func
            break
    
    assert run_task is not None
    assert run_task.is_async is True  # Task should be async


def test_map_function_with_body():
    """Test mapping function with statement body."""
    mapper = SVMapper()
    
    code = """
    class test;
        int result;
        
        function int compute(int a);
            result = a * 2;
            return result;
        endfunction
    endclass
    """
    
    result = mapper.map_text(code)
    
    assert result is True
    
    classes = mapper.get_classes()
    assert len(classes) == 1
    
    test_class = classes[0]
    
    # Find compute function
    compute_func = None
    for func in test_class.functions:
        if func.name == 'compute':
            compute_func = func
            break
    
    assert compute_func is not None
    assert len(compute_func.body) >= 1  # Should have at least assignment or return


def test_map_multiple_functions():
    """Test mapping multiple functions in a class."""
    mapper = SVMapper()
    
    code = """
    class calculator;
        function int add(int a, int b);
            return a + b;
        endfunction
        
        function int sub(int a, int b);
            return a - b;
        endfunction
        
        function int mul(int a, int b);
            return a * b;
        endfunction
    endclass
    """
    
    result = mapper.map_text(code)
    
    assert result is True
    
    classes = mapper.get_classes()
    assert len(classes) == 1
    
    calc_class = classes[0]
    
    # Should have at least 3 functions (plus any built-ins like randomize)
    func_names = [f.name for f in calc_class.functions]
    assert 'add' in func_names
    assert 'sub' in func_names
    assert 'mul' in func_names


def test_map_constructor():
    """Test mapping constructor function."""
    mapper = SVMapper()
    
    code = """
    class test;
        int x;
        
        function new(int init_val);
            x = init_val;
        endfunction
    endclass
    """
    
    result = mapper.map_text(code)
    
    assert result is True
    
    classes = mapper.get_classes()
    assert len(classes) == 1
    
    test_class = classes[0]
    
    # Find new function
    new_func = None
    for func in test_class.functions:
        if func.name == 'new':
            new_func = func
            break
    
    assert new_func is not None
    assert new_func.name == 'new'
    assert len(new_func.args.args) == 1


def test_function_with_conditional():
    """Test function with if statement."""
    mapper = SVMapper()
    
    code = """
    class test;
        function int abs(int x);
            if (x < 0)
                return -x;
            else
                return x;
        endfunction
    endclass
    """
    
    result = mapper.map_text(code)
    
    assert result is True
    
    classes = mapper.get_classes()
    test_class = classes[0]
    
    abs_func = None
    for func in test_class.functions:
        if func.name == 'abs':
            abs_func = func
            break
    
    assert abs_func is not None
    # Body should contain the if statement
    assert len(abs_func.body) > 0
