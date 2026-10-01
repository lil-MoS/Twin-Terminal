#!/bin/sh

PORT="${PORT:-7681}"

mkdir -p /data

cat > /data/.bashrc <<'EOF'
clear

printf '\033[1;36m'
echo "╭──────────────────────────────────────────────╮"
echo "│              ⚡ TWIN TERMINAL                │"
echo "╰──────────────────────────────────────────────╯"
printf '\033[0m'

printf '\033[1;32m'
echo "  Welcome to Twin Terminal"
printf '\033[0m'

echo ""
echo "  System : Alpine Linux"
echo "  Shell  : Bash"
echo "  Status : Online"
echo ""

PS1='\[\033[1;35m\]╭─[\[\033[1;36m\]\u@twin\[\033[1;35m\]]\n╰─\[\033[1;32m\]\w\[\033[1;35m\] \$ \[\033[0m\]'
EOF

exec ttyd \
    -W \
    -p "$PORT" \
    -t titleFixed="Twin Terminal" \
    -t fontSize=15 \
    -t fontFamily="'JetBrains Mono', 'Fira Code', monospace" \
    -t theme='{"background":"#0b0b12","foreground":"#e6e6f0","cursor":"#a855f7","selection":"#3b1d5c","black":"#11111b","red":"#ff5555","green":"#50fa7b","yellow":"#f1fa8c","blue":"#6272a4","magenta":"#bd93f9","cyan":"#8be9fd","white":"#f8f8f2"}' \
    bash --login
