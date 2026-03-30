#include <errno.h>
#include <limits.h>
#include <spawn.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include <CoreFoundation/CoreFoundation.h>
#include <mach-o/dyld.h>

extern char **environ;

#ifndef PYTHON_VERSION_STRING
#define PYTHON_VERSION_STRING "3.14"
#endif

static bool trim_last_component(char *path) {
    char *slash = strrchr(path, '/');
    if (slash == NULL || slash == path) {
        return false;
    }
    *slash = '\0';
    return true;
}

static int show_missing_runtime_alert(const char *bundle_path) {
    char command[4096];
    snprintf(
        command,
        sizeof(command),
        "/usr/bin/osascript -e 'display alert \"FreeWhisper bundle is incomplete\" "
        "message \"The embedded runtime is missing in %s. Rebuild or re-download FreeWhisper.app.\" as critical'",
        bundle_path
    );
    return system(command);
}

static bool path_exists(const char *path) {
    return access(path, F_OK) == 0;
}

static bool path_has_prefix(const char *path, const char *prefix) {
    size_t prefix_len = strlen(prefix);
    return strncmp(path, prefix, prefix_len) == 0
        && (path[prefix_len] == '\0' || path[prefix_len] == '/');
}

static bool is_inside_applications(const char *bundle_path) {
    if (path_has_prefix(bundle_path, "/Applications")) {
        return true;
    }

    const char *home = getenv("HOME");
    if (home == NULL || home[0] == '\0') {
        return false;
    }

    char user_apps[PATH_MAX];
    snprintf(user_apps, sizeof(user_apps), "%s/Applications", home);
    return path_has_prefix(bundle_path, user_apps);
}

static void app_support_path(char *buffer, size_t buffer_size, const char *leaf_name) {
    const char *home = getenv("HOME");
    if (home == NULL || home[0] == '\0') {
        buffer[0] = '\0';
        return;
    }

    snprintf(
        buffer,
        buffer_size,
        "%s/Library/Application Support/FreeWhisper/%s",
        home,
        leaf_name
    );
}

static void ensure_app_support_dir(void) {
    char dir_path[PATH_MAX];
    app_support_path(dir_path, sizeof(dir_path), "");
    size_t len = strlen(dir_path);
    if (len > 0 && dir_path[len - 1] == '/') {
        dir_path[len - 1] = '\0';
    }
    if (dir_path[0] == '\0') {
        return;
    }

    char partial[PATH_MAX];
    snprintf(partial, sizeof(partial), "%s", dir_path);
    for (char *cursor = partial + 1; *cursor != '\0'; ++cursor) {
        if (*cursor != '/') {
            continue;
        }
        *cursor = '\0';
        mkdir(partial, 0755);
        *cursor = '/';
    }
    mkdir(partial, 0755);
}

static bool move_prompt_already_seen(void) {
    char marker_path[PATH_MAX];
    app_support_path(marker_path, sizeof(marker_path), "move_to_applications_prompted");
    return marker_path[0] != '\0' && path_exists(marker_path);
}

static void mark_move_prompt_seen(void) {
    ensure_app_support_dir();
    char marker_path[PATH_MAX];
    app_support_path(marker_path, sizeof(marker_path), "move_to_applications_prompted");
    if (marker_path[0] == '\0') {
        return;
    }

    FILE *marker = fopen(marker_path, "w");
    if (marker == NULL) {
        return;
    }
    fputs("seen\n", marker);
    fclose(marker);
}

static bool is_dev_checkout(const char *bundle_path) {
    char parent_dir[PATH_MAX];
    snprintf(parent_dir, sizeof(parent_dir), "%s", bundle_path);
    if (!trim_last_component(parent_dir)) {
        return false;
    }

    char git_path[PATH_MAX];
    snprintf(git_path, sizeof(git_path), "%s/.git", parent_dir);
    return path_exists(git_path);
}

static bool ask_to_move_bundle_to_applications(const char *bundle_name) {
    char message[1024];
    snprintf(
        message,
        sizeof(message),
        "FreeWhisper works best from the Applications folder. Move \"%s\" to Applications now?",
        bundle_name
    );

    CFStringRef header = CFSTR("Move FreeWhisper to Applications?");
    CFStringRef text = CFStringCreateWithCString(
        kCFAllocatorDefault,
        message,
        kCFStringEncodingUTF8
    );
    if (text == NULL) {
        return false;
    }

    CFOptionFlags response = 0;
    SInt32 err = CFUserNotificationDisplayAlert(
        0,
        kCFUserNotificationNoteAlertLevel,
        NULL,
        NULL,
        NULL,
        header,
        text,
        CFSTR("Move to Applications"),
        CFSTR("Not Now"),
        NULL,
        &response
    );
    CFRelease(text);

    return err == 0 && response == kCFUserNotificationDefaultResponse;
}

static int run_process_and_wait(char *const argv[]) {
    pid_t child_pid = 0;
    int spawn_err = posix_spawn(&child_pid, argv[0], NULL, NULL, argv, environ);
    if (spawn_err != 0) {
        return spawn_err;
    }

    int status = 0;
    while (waitpid(child_pid, &status, 0) == -1) {
        if (errno != EINTR) {
            return errno;
        }
    }

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 1;
}

static int run_process_no_wait(char *const argv[]) {
    pid_t child_pid = 0;
    return posix_spawn(&child_pid, argv[0], NULL, NULL, argv, environ);
}

static bool move_bundle_to_applications(
    const char *bundle_path,
    char *new_bundle_path,
    size_t new_bundle_path_size
) {
    const char *bundle_name = strrchr(bundle_path, '/');
    bundle_name = (bundle_name == NULL) ? bundle_path : bundle_name + 1;

    snprintf(new_bundle_path, new_bundle_path_size, "/Applications/%s", bundle_name);
    if (path_exists(new_bundle_path)) {
        return false;
    }

    char *move_argv[] = {
        "/usr/bin/osascript",
        "-e", "on run argv",
        "-e", "set sourcePosix to item 1 of argv",
        "-e", "set targetDirPosix to item 2 of argv",
        "-e", "tell application \"Finder\"",
        "-e", "set sourceItem to POSIX file sourcePosix as alias",
        "-e", "set targetFolder to POSIX file targetDirPosix as alias",
        "-e", "move sourceItem to folder targetFolder",
        "-e", "end tell",
        "-e", "end run",
        "--",
        (char *)bundle_path,
        "/Applications",
        NULL,
    };

    return run_process_and_wait(move_argv) == 0 && path_exists(new_bundle_path);
}

int main(void) {
    uint32_t size = PATH_MAX;
    char executable_path[PATH_MAX];
    if (_NSGetExecutablePath(executable_path, &size) != 0) {
        fprintf(stderr, "FreeWhisper launcher: unable to resolve executable path\n");
        return 1;
    }

    char resolved_path[PATH_MAX];
    if (realpath(executable_path, resolved_path) == NULL) {
        fprintf(stderr, "FreeWhisper launcher: realpath failed: %s\n", strerror(errno));
        return 1;
    }

    char macos_dir[PATH_MAX];
    snprintf(macos_dir, sizeof(macos_dir), "%s", resolved_path);
    if (!trim_last_component(macos_dir)) {
        fprintf(stderr, "FreeWhisper launcher: unable to resolve MacOS dir\n");
        return 1;
    }

    char contents_dir[PATH_MAX];
    snprintf(contents_dir, sizeof(contents_dir), "%s", macos_dir);
    if (!trim_last_component(contents_dir)) {
        fprintf(stderr, "FreeWhisper launcher: unable to resolve bundle contents dir\n");
        return 1;
    }

    char bundle_path[PATH_MAX];
    snprintf(bundle_path, sizeof(bundle_path), "%s", contents_dir);
    if (!trim_last_component(bundle_path)) {
        fprintf(stderr, "FreeWhisper launcher: unable to resolve app bundle path\n");
        return 1;
    }

    const char *bundle_name = strrchr(bundle_path, '/');
    bundle_name = (bundle_name == NULL) ? bundle_path : bundle_name + 1;

    if (!is_inside_applications(bundle_path)
            && !is_dev_checkout(bundle_path)
            && !move_prompt_already_seen()) {
        if (ask_to_move_bundle_to_applications(bundle_name)) {
            char new_bundle_path[PATH_MAX];
            if (move_bundle_to_applications(
                    bundle_path,
                    new_bundle_path,
                    sizeof(new_bundle_path))) {
                char *open_argv[] = {
                    "/usr/bin/open",
                    "-n",
                    new_bundle_path,
                    NULL,
                };
                run_process_no_wait(open_argv);
                return 0;
            }
        } else {
            mark_move_prompt_seen();
        }
    }

    char python_home[PATH_MAX];
    char python_executable[PATH_MAX];
    char app_dir[PATH_MAX];
    char script_path[PATH_MAX];

    snprintf(
        python_home,
        sizeof(python_home),
        "%s/Frameworks/Python.framework/Versions/" PYTHON_VERSION_STRING,
        contents_dir
    );
    snprintf(
        python_executable,
        sizeof(python_executable),
        "%s/Frameworks/Python.framework/Versions/" PYTHON_VERSION_STRING "/Resources/Python.app/Contents/MacOS/Python",
        contents_dir
    );
    snprintf(app_dir, sizeof(app_dir), "%s/Resources/app", contents_dir);
    snprintf(script_path, sizeof(script_path), "%s/free_whisper.py", app_dir);

    if (access(python_executable, X_OK) != 0
            || access(script_path, R_OK) != 0
            || access(python_home, X_OK) != 0) {
        fprintf(stderr, "FreeWhisper launcher: runtime missing in %s\n", bundle_path);
        show_missing_runtime_alert(bundle_path);
        return 1;
    }

    setenv("PYTHONHOME", python_home, 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    setenv("PYTHONUNBUFFERED", "1", 1);

    const char *home = getenv("HOME");
    if (home != NULL && home[0] != '\0') {
        chdir(home);
    }

    char *argv[] = {
        python_executable,
        script_path,
        NULL,
    };
    int spawn_err = run_process_and_wait(argv);
    if (spawn_err != 0) {
        fprintf(stderr, "FreeWhisper launcher: child failed with code %d\n", spawn_err);
    }
    return spawn_err;
}
