#!/bin/bash
# Deploy production and staging from the host itself.
#
# Runs from a systemd timer, so nothing outside this machine can make it
# execute anything: it only reads the public repository. Production follows
# origin/main; staging follows the branch named in the "staging_branch" file
# of the deploy-state branch, which .github/workflows/staging.yml maintains.
#
# A deployed commit is recorded in STATE_DIR. A commit that fails to deploy is
# retried MAX_ATTEMPTS times (builds depend on openSUSE mirrors) and then left
# alone until a newer commit arrives.
#
# Installed by ansible/deploy.yml into ~/.local/bin rather than run from a
# checkout, so a branch deployed to staging cannot change this script.
set -uo pipefail

PROD_DIR=${PROD_DIR:-$HOME/powerlifting-clip-extractor}
STAGING_DIR=${STAGING_DIR:-$HOME/powerlifting-clip-extractor-staging}
STATE_DIR=${STATE_DIR:-$HOME/.local/state/powerlifting-deploy}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-3}

log() { echo "[$1] $2"; }

# Only code, not documentation, needs a rebuild and a restart.
only_docs_changed() {
    local dir=$1 old=$2 new=$3
    git -C "$dir" cat-file -e "$old^{commit}" 2>/dev/null || return 1
    git -C "$dir" diff --quiet "$old" "$new" -- . \
        ':(exclude,glob)**/*.md' ':(exclude)prompt.txt' ':(exclude).gitignore'
}

# Decide whether a commit has to be deployed; counts the attempts.
needs_deploy() {
    local env=$1 target=$2 failed="$STATE_DIR/$1.failed" count=0
    [ "$(cat "$STATE_DIR/$env" 2>/dev/null)" = "$target" ] && return 1
    if [ -f "$failed" ] && [ "$(cut -d' ' -f1 "$failed")" = "$target" ]; then
        count=$(cut -d' ' -f2 "$failed")
    fi
    if [ "$count" -ge "$MAX_ATTEMPTS" ]; then
        return 1
    fi
    echo "$target $((count + 1))" > "$failed"
    log "$env" "deploying $target (attempt $((count + 1)) of $MAX_ATTEMPTS)"
}

build_and_restart() {
    local dir=$1 image=$2 service=$3
    (cd "$dir" && podman build -t "$image" .) &&
        systemctl --user restart "$service" &&
        systemctl --user is-active "$service"
}

# Record the outcome once the working tree is at the target commit.
finish() {
    local env=$1 dir=$2 target=$3 image=$4 service=$5 previous
    previous=$(cat "$STATE_DIR/$env")
    if only_docs_changed "$dir" "$previous" "$target"; then
        log "$env" "documentation only, nothing to rebuild"
    elif ! build_and_restart "$dir" "$image" "$service"; then
        log "$env" "FAILED to deploy $target"
        return 1
    fi
    echo "$target" > "$STATE_DIR/$env"
    rm -f "$STATE_DIR/$env.failed"
    log "$env" "deployed $target"
}

deploy_production() {
    local target
    git -C "$PROD_DIR" fetch --quiet origin main || {
        log production "cannot fetch origin/main"
        return 1
    }
    target=$(git -C "$PROD_DIR" rev-parse FETCH_HEAD)
    [ -f "$STATE_DIR/production" ] ||
        git -C "$PROD_DIR" rev-parse HEAD > "$STATE_DIR/production"
    needs_deploy production "$target" || return 0
    git -C "$PROD_DIR" merge --quiet --ff-only "$target" || {
        log production "cannot fast-forward to $target"
        return 1
    }
    finish production "$PROD_DIR" "$target" \
        localhost/powerlifting:latest powerlifting
}

deploy_staging() {
    local branch target
    [ -d "$STAGING_DIR" ] || return 0
    git -C "$STAGING_DIR" fetch --quiet origin deploy-state || {
        log staging "cannot fetch deploy-state"
        return 1
    }
    branch=$(git -C "$STAGING_DIR" show FETCH_HEAD:staging_branch | tr -d '[:space:]')
    [ -n "$branch" ] && [ "$branch" != none ] || return 0
    # The name comes from the repository: never hand git anything but a
    # plain branch name.
    if ! [[ "$branch" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] ||
        ! git check-ref-format --branch "$branch" > /dev/null 2>&1; then
        log staging "ignoring invalid branch name"
        return 1
    fi
    git -C "$STAGING_DIR" fetch --quiet origin \
        "+refs/heads/$branch:refs/remotes/origin/$branch" || {
        log staging "branch $branch not found"
        return 0
    }
    target=$(git -C "$STAGING_DIR" rev-parse "refs/remotes/origin/$branch")
    [ -f "$STATE_DIR/staging" ] ||
        git -C "$STAGING_DIR" rev-parse HEAD > "$STATE_DIR/staging"
    needs_deploy staging "$target" || return 0
    # Branches get rebased and force-pushed, so this is not a fast-forward.
    git -C "$STAGING_DIR" checkout --quiet --force -B "$branch" \
        "refs/remotes/origin/$branch" || return 1
    finish staging "$STAGING_DIR" "$target" \
        localhost/powerlifting-staging:latest powerlifting-staging
}

mkdir -p "$STATE_DIR"
status=0
deploy_production || status=1
deploy_staging || status=1
exit $status
