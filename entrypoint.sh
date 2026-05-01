#!/bin/bash
set -e

# Inherit JAVA_HOME from eclipse-temurin image (e.g. /opt/java/openjdk).
# su - (login shell) drops environment variables, so we must pass it explicitly.
: "${JAVA_HOME:?JAVA_HOME must be set (e.g. /opt/java/openjdk)}"

# Set test user password from environment
if [ -n "$SSH_PASSWORD" ]; then
    echo "testuser:$SSH_PASSWORD" | chpasswd
fi

# Configure SSH: allow password auth, disable PAM to avoid host env issues
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config
sed -i 's/#PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config
sed -i 's/PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config
sed -i 's/UsePAM yes/UsePAM no/' /etc/ssh/sshd_config
sed -i 's/#KbdInteractiveAuthentication yes/KbdInteractiveAuthentication no/' /etc/ssh/sshd_config
sed -i 's/#ChallengeResponseAuthentication yes/ChallengeResponseAuthentication no/' /etc/ssh/sshd_config
echo "PermitRootLogin no" >> /etc/ssh/sshd_config

# Start SSH daemon
/usr/sbin/sshd

# Start sample Java application as testuser.
# Use sudo -u instead of su - to preserve JAVA_HOME / PATH.
sudo -u testuser \
    env "JAVA_HOME=${JAVA_HOME}" "PATH=${JAVA_HOME}/bin:${PATH}" \
    nohup java -jar /opt/math-game.jar > /dev/null 2>&1 &

# Verify Java process started
sleep 2
if pgrep -u testuser -f math-game >/dev/null; then
    echo "math-game.jar started successfully (PID=$(pgrep -u testuser -f math-game))"
else
    echo "WARNING: math-game.jar may not have started"
    echo "DEBUG: JAVA_HOME=${JAVA_HOME}"
    echo "DEBUG: testuser java path:"
    sudo -u testuser env "JAVA_HOME=${JAVA_HOME}" "PATH=${JAVA_HOME}/bin:${PATH}" which java || true
fi

# Keep container alive
tail -f /dev/null
