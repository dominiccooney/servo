# coding: utf8

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import os.path
import decisionlib
import functools
from decisionlib import CONFIG, SHARED
from urllib.request import urlopen


def main(task_for):
    with decisionlib.make_repo_bundle():
        tasks(task_for)


def tasks(task_for):
    if CONFIG.git_ref.startswith("refs/heads/"):
        branch = CONFIG.git_ref[len("refs/heads/"):]
        CONFIG.treeherder_repository_name = "servo-" + (
            branch if not branch.startswith("try-") else "try"
        )

    # Work around a tc-github bug/limitation:
    # https://bugzilla.mozilla.org/show_bug.cgi?id=1548781#c4
    if task_for.startswith("github"):
        # https://github.com/taskcluster/taskcluster/blob/21f257dc8/services/github/config.yml#L14
        CONFIG.routes_for_all_subtasks.append("statuses")

    if task_for == "github-push":
        all_tests = [
            linux_tidy_unit,
            linux_docs_check,
            windows_unit,
            windows_arm64,
            windows_uwp_x64,
            macos_unit,
            linux_release,
        ]
        by_branch_name = {
            "auto": all_tests,
            "try": all_tests,
            "try-taskcluster": [
                # Add functions here as needed, in your push to that branch
            ],
            "master": [
                upload_docs,
                layout_2020_regressions_report,
            ],

            # The "try-*" keys match those in `servo_try_choosers` in Homu’s config:
            # https://github.com/servo/saltfs/blob/master/homu/map.jinja

            "try-mac": [macos_unit],
            "try-linux": [linux_tidy_unit, linux_docs_check, linux_release],
            "try-windows": [windows_unit, windows_arm64, windows_uwp_x64],
            "try-arm": [windows_arm64],
            "try-wpt": [],
            "try-wpt-2020": [],
            "try-wpt-mac": [],
            "test-wpt": [],
        }
        by_branch_name["try-windows-rdp"] = [
            functools.partial(f, rdp=True) for f in by_branch_name["try-windows"]
        ]

        for function in by_branch_name.get(branch, []):
            function()

    elif task_for == "github-pull-request":
        CONFIG.treeherder_repository_name = "servo-prs"
        CONFIG.index_read_only = True
        CONFIG.docker_image_build_worker_type = None

        # We want the merge commit that GitHub creates for the PR.
        # The event does contain a `pull_request.merge_commit_sha` key, but it is wrong:
        # https://github.com/servo/servo/pull/22597#issuecomment-451518810
        CONFIG.git_sha_is_current_head()

        linux_tidy_unit_untrusted()

    elif task_for == "try-windows-ami":
        CONFIG.git_sha_is_current_head()
        CONFIG.windows_worker_type = os.environ["NEW_AMI_WORKER_TYPE"]
        windows_unit(cached=False, rdp=True)

    # https://tools.taskcluster.net/hooks/project-servo/daily
    elif task_for == "daily":
        daily_tasks_setup()
        with_rust_nightly()
        linux_nightly()
        windows_nightly()
        macos_nightly()
        update_wpt()
        uwp_nightly()


ping_on_daily_task_failure = "SimonSapin, nox, emilio"
build_artifacts_expire_in = "1 week"
build_dependencies_artifacts_expire_in = "1 month"
log_artifacts_expire_in = "1 year"

build_env = {
    "RUSTFLAGS": "-Dwarnings",
    "CARGO_INCREMENTAL": "0",
}
unix_build_env = {
}
linux_build_env = {
    "RUST_BACKTRACE": "1", # https://github.com/servo/servo/issues/26192
    "SHELL": "/bin/dash",  # For SpiderMonkey’s build system
    "CCACHE": "sccache",
    "RUSTC_WRAPPER": "sccache",
    "CC": "clang",
    "CXX": "clang++",
    "SCCACHE_IDLE_TIMEOUT": "1200",
    # https://github.com/servo/servo/issues/24714#issuecomment-552951519
    "SCCACHE_MAX_FRAME_LENGTH": str(100 * 1024 * 1024),  # 100 MiB
}
macos_build_env = {}
windows_build_env = {
    "x86_64": {
        "GSTREAMER_1_0_ROOT_X86_64": "%HOMEDRIVE%%HOMEPATH%\\gst\\gstreamer\\1.0\\x86_64\\",
    },
    "arm64": {
        "PKG_CONFIG_ALLOW_CROSS": "1",
    },
    "all": {
        "PYTHON3": "%HOMEDRIVE%%HOMEPATH%\\python3\\python.exe",
        "LINKER": "lld-link.exe",
        "MOZTOOLS_PATH_PREPEND": "%HOMEDRIVE%%HOMEPATH%\\git\\cmd",
        "CC": "clang-cl.exe",
        "CXX": "clang-cl.exe",
    },
}

windows_sparse_checkout = [
    "/*",
    "!/tests/wpt/metadata",
    "!/tests/wpt/mozilla",
    "!/tests/wpt/webgl",
    "!/tests/wpt/web-platform-tests",
    "/tests/wpt/web-platform-tests/tools",
]


def linux_tidy_unit_untrusted():
    return (
        decisionlib.DockerWorkerTask("Tidy + dev build + unit tests")
        .with_worker_type("docker-untrusted")
        .with_treeherder("Linux x64", "Tidy+Unit")
        .with_max_run_time_minutes(60)
        .with_dockerfile(dockerfile_path("build"))
        .with_env(**build_env, **unix_build_env, **linux_build_env)
        .with_repo_bundle()
        .with_script("""
            python3 ./mach test-tidy --no-progress --all
            python3 ./mach test-tidy --no-progress --self-test
            python3 ./mach bootstrap-gstreamer
            python3 ./mach build --dev
            python3 ./mach test-unit

            ./etc/ci/lockfile_changed.sh
            ./etc/memory_reports_over_time.py --test
            ./etc/ci/check_no_panic.sh
        """)
        .create()
    )


def linux_tidy_unit():
    return (
        linux_build_task("Tidy + dev build + unit tests")
        .with_treeherder("Linux x64", "Tidy+Unit")
        .with_max_run_time_minutes(75)
        .with_script("""
            python3 ./mach test-tidy --no-progress --all
            python3 ./mach build --dev
            python3 ./mach test-unit
            python3 ./mach package --dev
            python3 ./mach build --dev --features refcell_backtrace
            python3 ./mach build --dev --features layout-2020
            python3 ./mach build --dev --libsimpleservo
            python3 ./mach build --dev -p servo-gst-plugin
            python3 ./mach build --dev --media-stack=dummy
            python3 ./mach test-tidy --no-progress --self-test

            ./etc/memory_reports_over_time.py --test
            ./etc/taskcluster/mock.py
            ./etc/ci/lockfile_changed.sh
            ./etc/ci/check_no_panic.sh
        """)
        .find_or_create("linux_unit." + CONFIG.tree_hash())
    )


def linux_docs_check():
    return (
        linux_build_task("Docs + check")
        .with_treeherder("Linux x64", "Doc+Check")
        .with_script("""
            RUSTDOCFLAGS="--disable-minification" python3 ./mach doc
            (
                cd target/doc
                git init
                git add .
                git -c user.name="Taskcluster" -c user.email="" \
                    commit -q -m "Rebuild Servo documentation"
                git bundle create docs.bundle HEAD
            )

        """
        # Because `rustdoc` needs metadata of dependency crates,
        # `cargo doc` does almost all of the work that `cargo check` does.
        # Therefore, when running them in this order the second command does very little
        # and should finish quickly.
        # The reverse order would not increase the total amount of work to do,
        # but would reduce the amount of parallelism available.
        """
            python3 ./mach check
        """)
        .with_artifacts("/repo/target/doc/docs.bundle")
        .find_or_create("docs." + CONFIG.tree_hash())
    )


def upload_docs():
    docs_build_task_id = decisionlib.Task.find("docs." + CONFIG.tree_hash())
    return (
        linux_task("Upload docs to GitHub Pages")
        .with_treeherder("Linux x64", "DocUpload")
        .with_dockerfile(dockerfile_path("base"))
        .with_curl_artifact_script(docs_build_task_id, "docs.bundle")
        .with_features("taskclusterProxy")
        .with_scopes("secrets:get:project/servo/doc.servo.org")
        .with_env(PY="""if 1:
            import urllib.request, json, os
            root_url = os.environ["TASKCLUSTER_PROXY_URL"]
            url = root_url + "/api/secrets/v1/secret/project/servo/doc.servo.org"
            token = json.load(urllib.request.urlopen(url))["secret"]["token"]
            open("/root/.git-credentials", "w").write("https://git:%s@github.com/" % token)
        """)
        .with_script("""
            python3 -c "$PY"
            git init --bare
            git config credential.helper store
            git fetch --quiet docs.bundle
            git push --force https://github.com/servo/doc.servo.org FETCH_HEAD:gh-pages
        """)
        .create()
    )


def layout_2020_regressions_report():
    return (
        linux_task("Layout 2020 regressions report")
        .with_treeherder("Linux x64", "RegressionsReport")
        .with_dockerfile(dockerfile_path("base"))
        .with_repo_bundle()
        .with_script(
            "python3 tests/wpt/reftests-report/gen.py %s %s"
            % (CONFIG.tree_hash(), CONFIG.git_sha)
        )
        .with_index_and_artifacts_expire_in(log_artifacts_expire_in)
        .with_artifacts("/repo/tests/wpt/reftests-report/report.html")
        .with_index_at("layout-2020-regressions-report")
        .create()
    )

def macos_unit():
    return (
        macos_build_task("Dev build + unit tests")
        .with_treeherder("macOS x64", "Unit")
        .with_script("""
            python3 ./mach build --dev --verbose
            python3 ./mach test-unit
            python3 ./mach package --dev
            ./etc/ci/macos_package_smoketest.sh target/debug/servo-tech-demo.dmg
            ./etc/ci/lockfile_changed.sh
        """)
        .find_or_create("macos_unit." + CONFIG.tree_hash())
    )


def with_rust_nightly():
    modified_build_env = dict(build_env)
    flags = modified_build_env.pop("RUSTFLAGS").split(" ")
    flags.remove("-Dwarnings")
    if flags:  # pragma: no cover
        modified_build_env["RUSTFLAGS"] = " ".join(flags)

    return (
        linux_build_task("with Rust Nightly", build_env=modified_build_env)
        .with_treeherder("Linux x64", "RustNightly")
        .with_script("""
            echo "nightly" > rust-toolchain
            python3 ./mach build --dev
            python3 ./mach test-unit
        """)
        .create()
    )


appx_artifact = '/'.join([
    'repo',
    'support',
    'hololens',
    'AppPackages',
    'ServoApp',
    'FirefoxReality.zip',
])


def windows_arm64(rdp=False):
    return (
        windows_build_task("UWP dev build", arch="arm64", package=False, rdp=rdp)
        .with_treeherder("Windows arm64", "UWP-Dev")
        .with_features("taskclusterProxy")
        .with_scopes("secrets:get:project/servo/windows-codesign-cert/latest")
        .with_script(
            "python mach build --dev --target=aarch64-uwp-windows-msvc",
            "python mach package --dev --target aarch64-uwp-windows-msvc --uwp=arm64",
        )
        .with_artifacts(appx_artifact)
        .find_or_create("build.windows_uwp_arm64_dev." + CONFIG.tree_hash())
    )


def windows_uwp_x64(rdp=False):
    return (
        windows_build_task("UWP dev build", package=False, rdp=rdp)
        .with_treeherder("Windows x64", "UWP-Dev")
        .with_features("taskclusterProxy")
        .with_scopes("secrets:get:project/servo/windows-codesign-cert/latest")
        .with_script(
            "python mach build --dev --target=x86_64-uwp-windows-msvc",
            "python mach package --dev --target=x86_64-uwp-windows-msvc --uwp=x64",
            "python mach test-tidy --force-cpp --no-wpt",
        )
        .with_artifacts(appx_artifact)
        .find_or_create("build.windows_uwp_x64_dev." + CONFIG.tree_hash())
    )


def uwp_nightly(rdp=False):
    return (
        windows_build_task("Nightly UWP build and upload", package=False, rdp=rdp)
        .with_treeherder("Windows x64", "UWP-Nightly")
        .with_features("taskclusterProxy")
        .with_scopes(
            "secrets:get:project/servo/s3-upload-credentials",
            "secrets:get:project/servo/windows-codesign-cert/latest",
        )
        .with_script(
            "python mach build --release --target=x86_64-uwp-windows-msvc",
            "python mach build --release --target=aarch64-uwp-windows-msvc",
            "python mach package --release --target=x86_64-uwp-windows-msvc --uwp=x64 --uwp=arm64",
            "python mach upload-nightly uwp --secret-from-taskcluster",
        )
        .with_artifacts(appx_artifact)
        .with_max_run_time_minutes(3 * 60)
        .find_or_create("build.windows_uwp_nightlies." + CONFIG.tree_hash())
    )


def windows_unit(cached=True, rdp=False):
    task = (
        windows_build_task("Dev build + unit tests", rdp=rdp)
        .with_treeherder("Windows x64", "Unit")
        .with_script(
            # Not necessary as this would be done at the start of `build`,
            # but this allows timing it separately.
            "python mach fetch",

            "python mach build --dev",

            "python mach test-unit",
            # Running the TC task with administrator privileges breaks the
            # smoketest for unknown reasons.
            #"python mach smoketest --angle",

            "python mach package --dev",
            "python mach build --dev --libsimpleservo",
            # The GStreamer plugin currently doesn't support Windows
            # https://github.com/servo/servo/issues/25353
            # "mach build --dev -p servo-gst-plugin",

        )
        .with_artifacts("repo/target/debug/msi/Servo.exe",
                        "repo/target/debug/msi/Servo.zip")
    )
    if cached:
        return task.find_or_create("build.windows_x64_dev." + CONFIG.tree_hash())
    else:
        return task.create()


def windows_nightly(rdp=False):
    return (
        windows_build_task("Nightly build and upload", rdp=rdp)
        .with_treeherder("Windows x64", "Nightly")
        .with_features("taskclusterProxy")
        .with_scopes("secrets:get:project/servo/s3-upload-credentials")
        .with_script("python mach fetch",
                     "python mach build --release",
                     "python mach package --release",
                     "python mach upload-nightly windows-msvc --secret-from-taskcluster")
        .with_artifacts("repo/target/release/msi/Servo.exe",
                        "repo/target/release/msi/Servo.zip")
        .find_or_create("build.windows_x64_nightly." + CONFIG.tree_hash())
    )


def linux_nightly():
    return (
        linux_build_task("Nightly build and upload")
        .with_treeherder("Linux x64", "Nightly")
        .with_features("taskclusterProxy")
        .with_scopes("secrets:get:project/servo/s3-upload-credentials")
        # Not reusing the build made for WPT because it has debug assertions
        .with_script(
            "python3 ./mach build --release",
            "python3 ./mach package --release",
            "python3 ./mach upload-nightly linux --secret-from-taskcluster",
        )
        .with_artifacts("/repo/target/release/servo-tech-demo.tar.gz")
        .find_or_create("build.linux_x64_nightly" + CONFIG.tree_hash())
    )


def linux_release():
    return (
        linux_build_task("Release build")
        .with_treeherder("Linux x64", "Release")
        .with_script(
            "python3 ./mach build --release",
            "python3 ./mach package --release",
        )
        .find_or_create("build.linux_x64_release" + CONFIG.tree_hash())
    )


def macos_nightly():
    return (
        macos_build_task("Nightly build and upload")
        .with_treeherder("macOS x64", "Nightly")
        .with_features("taskclusterProxy")
        .with_scopes(
            "secrets:get:project/servo/s3-upload-credentials",
            "secrets:get:project/servo/github-homebrew-token",
        )
        .with_script(
            "python3 ./mach build --release",
            "python3 ./mach package --release",
            "./etc/ci/macos_package_smoketest.sh target/release/servo-tech-demo.dmg",
            "python3 ./mach upload-nightly mac --secret-from-taskcluster",
        )
        .with_artifacts("repo/target/release/servo-tech-demo.dmg")
        .find_or_create("build.mac_x64_nightly." + CONFIG.tree_hash())
    )


def update_wpt():
    build_task = linux_release_build_with_debug_assertions(layout_2020=False)
    return (
        linux_task("WPT update")
        .with_treeherder("Linux x64", "WPT-update")
        .with_dockerfile(dockerfile_path("wpt-update"))
        .with_features("taskclusterProxy")
        .with_scopes("secrets:get:project/servo/wpt-sync")
        .with_index_and_artifacts_expire_in(log_artifacts_expire_in)
        .with_max_run_time_minutes(8 * 60)
        # Not using the bundle, pushing the new changes to the git remote requires a full repo.
        .with_repo()
        .with_curl_artifact_script(build_task, "target.tar.gz")
        .with_script("""
            tar -xzf target.tar.gz
            # Use `cat` to force wptrunner’s non-interactive mode
            ./etc/ci/update-wpt-checkout fetch-and-update-expectations | cat
            ./etc/ci/update-wpt-checkout open-pr
            ./etc/ci/update-wpt-checkout cleanup
        """)
        .create()
    )


def macos_release_build_with_debug_assertions(priority=None):
    return (
        macos_build_task("Release build, with debug assertions")
        .with_treeherder("macOS x64", "Release+A")
        .with_priority(priority)
        .with_script("\n".join([
            "python3 ./mach build --release --verbose --with-debug-assertions",
            "./etc/ci/lockfile_changed.sh",
            "tar -czf target.tar.gz" +
            " target/release/servo" +
            " target/release/*.dylib" +
            " resources",
        ]))
        .with_artifacts("repo/target.tar.gz")
        .find_or_create("build.macos_x64_release_w_assertions." + CONFIG.tree_hash())
    ) # pragma: no cover


def linux_release_build_with_debug_assertions(layout_2020):
    if layout_2020:
        name_prefix = "Layout 2020 "      # pragma: no cover
        build_args = "--with-layout-2020" # pragma: no cover
        index_key_suffix = "_2020"        # pragma: no cover
        treeherder_prefix = "2020-"       # pragma: no cover
    else:
        name_prefix = ""
        build_args = ""
        index_key_suffix = ""
        treeherder_prefix = ""
    return (
        linux_build_task(name_prefix + "Release build, with debug assertions")
        .with_treeherder("Linux x64", treeherder_prefix + "Release+A")
        .with_script("""
            time python3 ./mach rustc -V
            time python3 ./mach fetch
            python3 ./mach build --release --with-debug-assertions %s -p servo
            ./etc/ci/lockfile_changed.sh
            tar -czf /target.tar.gz \
                target/release/servo \
                resources
            sccache --show-stats
        """ % build_args)
        .with_artifacts("/target.tar.gz")
        .find_or_create("build.linux_x64%s_release_w_assertions.%s" % (
            index_key_suffix,
            CONFIG.tree_hash(),
        ))
    )

def daily_tasks_setup():
    # Unlike when reacting to a GitHub push event,
    # the commit hash is not known until we clone the repository.
    CONFIG.git_sha_is_current_head()

    # On failure, notify a few people on IRC
    # https://docs.taskcluster.net/docs/reference/core/taskcluster-notify/docs/usage
    notify_route = "notify.irc-channel.#servo.on-failed"
    CONFIG.routes_for_all_subtasks.append(notify_route)
    CONFIG.scopes_for_all_subtasks.append("queue:route:" + notify_route)
    CONFIG.task_name_template = "Servo daily: %s. On failure, ping: " + ping_on_daily_task_failure


def dockerfile_path(name):
    return os.path.join(os.path.dirname(__file__), "docker", name + ".dockerfile")


def linux_task(name):
    return (
        decisionlib.DockerWorkerTask(name)
        .with_worker_type("docker")
        .with_treeherder_required()
    )


def windows_task(name):
    return (
        decisionlib.WindowsGenericWorkerTask(name)
        .with_worker_type(CONFIG.windows_worker_type)
        .with_treeherder_required()
    )


def macos_task(name):
    return (
        decisionlib.MacOsGenericWorkerTask(name)
        .with_worker_type(CONFIG.macos_worker_type)
        .with_treeherder_required()
    )


def linux_build_task(name, *, build_env=build_env):
    task = (
        linux_task(name)
        # https://docs.taskcluster.net/docs/reference/workers/docker-worker/docs/caches
        .with_scopes("docker-worker:cache:servo-*")
        .with_caches(**{
            "servo-cargo-registry": "/root/.cargo/registry",
            "servo-cargo-git": "/root/.cargo/git",
            "servo-rustup": "/root/.rustup",
            "servo-sccache": "/root/.cache/sccache",
            "servo-gradle": "/root/.gradle",
        })
        .with_index_and_artifacts_expire_in(build_artifacts_expire_in)
        .with_max_run_time_minutes(60)
        .with_dockerfile(dockerfile_path("build"))
        .with_env(**build_env, **unix_build_env, **linux_build_env)
        .with_repo_bundle()
        .with_script("python3 ./mach bootstrap-gstreamer")
    )
    return task


def windows_build_task(name, package=True, arch="x86_64", rdp=False):
    hashes = {
        "devel": {
            "x86_64": "bd444f3ff9d828f93ba5db0ef511d648d238fff50c4435ccefc7b3e9b2bea3b9",
        },
        "non-devel": {
            "x86_64": "f33fff17a558a433b9c4cf7bd9a338a3d0867fa2d5ee1ee33d249b6a55e8a297",
        },
    }
    prefix = {
        "x86_64": "msvc",
    }
    version = "1.16.2"
    task = (
        windows_task(name)
        .with_max_run_time_minutes(90)
        .with_env(
            **build_env,
            **windows_build_env[arch],
            **windows_build_env["all"]
        )
        .with_repo_bundle(sparse_checkout=windows_sparse_checkout)
        .with_python3()
        .with_rustup()
    )
    if arch in hashes["non-devel"] and arch in hashes["devel"]:
        task.with_repacked_msi(
            url=("https://gstreamer.freedesktop.org/data/pkg/windows/" +
                 "%s/gstreamer-1.0-%s-%s-%s.msi" % (version, prefix[arch], arch, version)),
            sha256=hashes["non-devel"][arch],
            path="gst",
        )
        task.with_repacked_msi(
            url=("https://gstreamer.freedesktop.org/data/pkg/windows/" +
                 "%s/gstreamer-1.0-devel-%s-%s-%s.msi" % (version, prefix[arch], arch, version)),
            sha256=hashes["devel"][arch],
            path="gst",
        )
    if package:
        task.with_directory_mount(
            "https://github.com/wixtoolset/wix3/releases/download/wix3111rtm/wix311-binaries.zip",
            sha256="37f0a533b0978a454efb5dc3bd3598becf9660aaf4287e55bf68ca6b527d051d",
            path="wix",
        )
        task.with_path_from_homedir("wix")
    if rdp:
        task.with_rdp_info(artifact_name="project/servo/rdp-info")
    return task


def with_homebrew(task, brewfiles):
    for brewfile in brewfiles:
        task.with_script("time brew bundle install --verbose --no-upgrade --file=" + brewfile)
    return task


def macos_build_task(name):
    build_task = (
        macos_task(name)
        # Stray processes eating CPU can slow things down:
        # https://github.com/servo/servo/issues/24735
        .with_max_run_time_minutes(60 * 2)
        .with_env(**build_env, **unix_build_env, **macos_build_env)
        .with_repo_bundle(alternate_object_dir="/var/cache/servo.git/objects")
        .with_python3()
        .with_rustup()
        .with_index_and_artifacts_expire_in(build_artifacts_expire_in)
        # Debugging for surprising generic-worker behaviour
        .with_early_script("ls")
        .with_script("ls target || true")
    )
    return (
        with_homebrew(build_task, [
            "etc/taskcluster/macos/Brewfile",
            "etc/taskcluster/macos/Brewfile-build",
        ])
        .with_script("""
            export OPENSSL_INCLUDE_DIR="$(brew --prefix openssl)/include"
            export OPENSSL_LIB_DIR="$(brew --prefix openssl)/lib"
            export PKG_CONFIG_PATH="$(brew --prefix libffi)/lib/pkgconfig/"
            export PKG_CONFIG_PATH="$(brew --prefix zlib)/lib/pkgconfig/:$PKG_CONFIG_PATH"
        """)

        .with_directory_mount(
            "https://github.com/mozilla/sccache/releases/download/"
                "0.2.7/sccache-0.2.7-x86_64-apple-darwin.tar.gz",
            sha256="f86412abbbcce2d3f23e7d33305469198949f5cf807e6c3258c9e1885b4cb57f",
            path="sccache",
        )
        # Early script in order to run with the initial $PWD
        .with_early_script("""
            export PATH="$PWD/sccache/sccache-0.2.7-x86_64-apple-darwin:$PATH"
        """)
        # sccache binaries requires OpenSSL 1.1 and are not compatible with 1.0.
        # "Late" script in order to run after Homebrew is installed.
        .with_script("""
            time brew install openssl@1.1
            export DYLD_LIBRARY_PATH="$HOME/homebrew/opt/openssl@1.1/lib"
        """)
    )


CONFIG.task_name_template = "Servo: %s"
CONFIG.docker_images_expire_in = build_dependencies_artifacts_expire_in
CONFIG.repacked_msi_files_expire_in = build_dependencies_artifacts_expire_in
CONFIG.index_prefix = "project.servo"
CONFIG.default_provisioner_id = "proj-servo"
CONFIG.docker_image_build_worker_type = "docker"

CONFIG.windows_worker_type = "win2016"
CONFIG.macos_worker_type = "macos"

if __name__ == "__main__":  # pragma: no cover
    main(task_for=os.environ["TASK_FOR"])
