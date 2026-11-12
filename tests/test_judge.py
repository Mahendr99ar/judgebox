"""End to end through the judge on the sample problems, in all three languages."""
import pytest

from judgebox.judge.core import judge
from judgebox.judge.problem import ProblemError, load
from tests.conftest import PROBLEMS, needs_root

pytestmark = needs_root

CPP_SUM = '#include <cstdio>\nint main(){long long a,b;scanf("%lld %lld",&a,&b);printf("%lld\\n",a+b);}'
CPP_SUM_INT = '#include <cstdio>\nint main(){int a,b;scanf("%d %d",&a,&b);printf("%d\\n",a+b);}'
JAVA_SUM = (
    "import java.util.*;public class Main{public static void main(String[] x){"
    "Scanner s=new Scanner(System.in);System.out.println(s.nextLong()+s.nextLong());}}"
)
CPP_SORT = (
    "#include <bits/stdc++.h>\nint main(){int n;scanf(\"%d\",&n);std::vector<int>a(n);"
    "for(auto&x:a)scanf(\"%d\",&x);std::sort(a.begin(),a.end());for(int x:a)printf(\"%d \",x);}"
)
CPP_BUBBLE = (
    "#include <bits/stdc++.h>\nint main(){int n;scanf(\"%d\",&n);std::vector<int>a(n);"
    "for(auto&x:a)scanf(\"%d\",&x);for(int i=0;i<n;i++)for(int j=0;j+1<n-i;j++)"
    "if(a[j]>a[j+1])std::swap(a[j],a[j+1]);for(int x:a)printf(\"%d \",x);}"
)
PY_TWO_SUM = """
n, t = map(int, input().split())
seen = {}
for i, x in enumerate(map(int, input().split())):
    if t - x in seen:
        print(seen[t - x] + 1, i + 1)
        break
    seen[x] = i
"""


def problem(slug):
    return load(PROBLEMS / slug)


@pytest.mark.parametrize("slug, lang, source, verdict", [
    ("a-plus-b", "cpp17", CPP_SUM, "AC"),
    ("a-plus-b", "cpp17", CPP_SUM_INT, "WA"),
    ("a-plus-b", "python3", "a, b = map(int, input().split())\nprint(a + b)", "AC"),
    ("a-plus-b", "java21", JAVA_SUM, "AC"),
    ("a-plus-b", "cpp17", "int main( { }", "CE"),
    ("a-plus-b", "python3", "print(", "CE"),
    ("a-plus-b", "python3", "print(1 // 0)", "RE"),
    ("sort-numbers", "cpp17", CPP_SORT, "AC"),
    ("sort-numbers", "cpp17", CPP_BUBBLE, "TLE"),
    ("circle-area", "python3", "import math\nr = float(input())\nprint(math.pi * r * r)", "AC"),
    ("circle-area", "python3", "r = float(input())\nprint(3.14 * r * r)", "WA"),
    ("two-sum", "python3", PY_TWO_SUM, "AC"),
    ("two-sum", "python3", "print(1, 2)", "WA"),
])
def test_verdicts(slug, lang, source, verdict):
    outcome = judge(problem(slug), lang, source)
    assert outcome.verdict == verdict, outcome


def test_stops_at_first_failing_test_and_reports_it():
    outcome = judge(problem("a-plus-b"), "cpp17", CPP_SUM_INT)
    assert [t.verdict for t in outcome.tests] == ["AC", "AC", "WA"]
    assert outcome.message.startswith("test 3")


def test_compile_error_message_is_returned():
    outcome = judge(problem("a-plus-b"), "cpp17", "int main() { return x; }")
    assert outcome.verdict == "CE"
    assert "'x' was not declared" in outcome.compile_message or "x" in outcome.compile_message


def test_events_are_emitted_per_test():
    events = []
    judge(problem("a-plus-b"), "cpp17", CPP_SUM, emit=events.append)
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["compiling", "compiled"]
    assert kinds.count("test") == len(problem("a-plus-b").tests)


def test_bad_package_is_rejected(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "problem.toml").write_text('checker = "magic"')
    with pytest.raises(ProblemError):
        load(tmp_path / "broken")
