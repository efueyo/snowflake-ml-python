workspace(name = "SnowML")

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

http_archive(
    name = "bazel_skylib",
    sha256 = "b8a1527901774180afc798aeb28c4634bdccf19c4d98e7bdd1ce79d1fe9aaad7",
    urls = [
        "https://mirror.bazel.build/github.com/bazelbuild/bazel-skylib/releases/download/1.4.1/bazel-skylib-1.4.1.tar.gz",
        "https://github.com/bazelbuild/bazel-skylib/releases/download/1.4.1/bazel-skylib-1.4.1.tar.gz",
    ],
)

load("@bazel_skylib//:workspace.bzl", "bazel_skylib_workspace")

bazel_skylib_workspace()
# Latest @ 2022-10-18. Version 0.13.0 released 2022-09-25
http_archive(
    name = "rules_python",
    sha256 = "8c8fe44ef0a9afc256d1e75ad5f448bb59b81aba149b8958f02f7b3a98f5d9b4",
    strip_prefix = "rules_python-0.13.0",
    url = "https://github.com/bazelbuild/rules_python/archive/refs/tags/0.13.0.tar.gz",
)

load("//third_party/rules_conda:defs.bzl", "conda_create", "load_conda", "register_toolchain")

# Below two conda environments (toolchains) are created and they require different
# constraint values. Two platforms defined in bazel/platforms/BUILD provide those
# constraint values. A toolchain matches a platform as long as the platform provides
# all the constraint values the toolchain requires, which means:
# - py3_toolchain_snowflake_conda_only is used iff
#   //bazel/platforms:snowflake_conda_env is the target platform
# - py3_toolchain_extended_channels is used iff
#   //bazel/platforms:extended_conda_env is the target platform
#
# The default platform when --platforms flag is not set, is specified in
# .bazelrc .

load_conda(conda_repo_name = "snowflake_conda", quiet = False)

conda_create(
    name = "py3_env_snowflake_conda_only",
    conda_repo_name = "snowflake_conda",
    timeout = 3600,
    clean = False,
    environment = "@//:conda-env-snowflake.yml",
    quiet = False,
)

register_toolchain(
    name = "py3_env_snowflake_conda_only_repo",
    env = "py3_env_snowflake_conda_only",
    target_compatible_with=["@SnowML//bazel/platforms:snowflake_conda_channel"],
    toolchain_name = "py3_toolchain_snowflake_conda_only",
)

load_conda(conda_repo_name = "extended_conda", quiet = False)

conda_create(
    name = "py3_env_extended_channels",
    conda_repo_name = "extended_conda",
    timeout = 3600,
    clean = False,
    environment = "@//:conda-env.yml",
    quiet = False,
)

register_toolchain(
    name = "py3_env_extended_channels_repo",
    env = "py3_env_extended_channels",
    target_compatible_with=["@SnowML//bazel/platforms:extended_conda_channels"],
    toolchain_name = "py3_toolchain_extended_channels",
)