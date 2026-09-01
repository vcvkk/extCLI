# extCLI self-test
#
# Runs every command the console has and reports what worked. Written in
# extCLI's own shell on purpose: it exercises functions, "$@", $?, arithmetic
# and redirection on the way past, so a failure here is as likely to be the
# shell's fault as the command's — which is the point.
#
# Read it. Edit it. It is a plain file: `host paths` says where.
#
# Only one chat is ever written to, and only these two lines do it:
#   tg send $chat ...
# Change $chat below if you want them somewhere else.

chat="@JettaXP"

passed=0
failed=0
work="$HOME/selftest"

check() {
    "$@" > /dev/null
    if test $? = 0; then
        passed=$((passed + 1))
        echo "  ok    $*"
    else
        failed=$((failed + 1))
        echo "  FAIL  $*"
    fi
}

note() {
    # for a line that cannot go through check() — a redirection would apply to
    # the function call itself, and the report would end up inside the file
    if test $? = 0; then
        passed=$((passed + 1))
        echo "  ok    $*"
    else
        failed=$((failed + 1))
        echo "  FAIL  $*"
    fi
}

refuses() {
    "$@" > /dev/null
    if test $? = 0; then
        failed=$((failed + 1))
        echo "  FAIL  $* was accepted and should not have been"
    else
        passed=$((passed + 1))
        echo "  ok    $* refused"
    fi
}

echo "extCLI self-test"
echo ""

echo "console"
check help
check help tg send
check history
check clip

echo ""
echo "host"
check host status
check host version
check host paths
check host check --window
check host check

echo ""
echo "plugins"
check plugin list
check plugin list --enabled
check plugin info extcli
check plugin path extcli
check plugin config extcli

echo ""
echo "client settings"
check config stores
check config list
check config search theme

echo ""
echo "log"
check log tail
check log tail -n 5
check log grep console

echo ""
echo "shell"
check echo hello
check pwd
check env
check set
check which echo
check true
check test -d /
check host backends

echo ""
echo "variables and aliases"
check export EXTCLI_SELFTEST=1
check unset EXTCLI_SELFTEST
check alias selftest_alias='echo aliased'
check selftest_alias
check unalias selftest_alias

echo ""
echo "files"
check mkdir -p $work
cd $work
note "cd $work"
check touch probe.txt
echo contents > probe.txt
note "echo contents > probe.txt"
check cat probe.txt
check ls
check ls -l
check cp probe.txt copy.txt
check mv copy.txt moved.txt
check head probe.txt
check tail probe.txt
check wc probe.txt
check grep contents probe.txt
check rm moved.txt

echo ""
echo "the device's own tools"
check uname -a
check id
check date
check df

echo ""
echo "chats"
check tg chats JettaXP
check tg read $chat -n 3
check tg read $chat -n 3 --oneline
check tg id $chat
check tg send $chat extCLI self-test: text
check tg send $chat --file probe.txt extCLI self-test: file

echo ""
echo "rootfs"
check rootfs status
check rootfs probe exec

echo ""
echo "things that must be refused"
refuses tg send
refuses tg send $chat
refuses plugin info no_such_plugin_here
refuses plugin install /no/such/file.eaf
refuses source /no/such/script.sh
refuses config get no_such_setting_here
refuses nosuchcommand

cd $HOME
rm -f $work/probe.txt

echo ""
echo "$passed passed, $failed failed"
