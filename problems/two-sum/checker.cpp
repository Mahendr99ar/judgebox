// Any pair of distinct 1-based indices whose values add up to the target is accepted.
#include <cstdio>
#include <vector>
int main(int argc, char** argv) {
    FILE* in = fopen(argv[1], "r");
    FILE* out = fopen(argv[2], "r");
    long long n, target;
    if (fscanf(in, "%lld %lld", &n, &target) != 2) return 3;
    std::vector<long long> a(n);
    for (auto& x : a) fscanf(in, "%lld", &x);
    long long i, j;
    if (fscanf(out, "%lld %lld", &i, &j) != 2) { puts("expected two indices"); return 1; }
    if (i < 1 || j < 1 || i > n || j > n || i == j) { puts("indices out of range or equal"); return 1; }
    if (a[i - 1] + a[j - 1] != target) { puts("values do not add up to the target"); return 1; }
    return 0;
}
