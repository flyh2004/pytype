# Copyright (c) 2016 Google Inc. (under http://www.apache.org/licenses/LICENSE-2.0)

# should produce signature mismatch
def f1(x : e1) -> r1: ...

# properly annotated, but currently ignored
def f1b((x : e1, y : e2)) -> r1: ...

# should produce signature mismatch
def f2(x : e2) -> r2: ...
