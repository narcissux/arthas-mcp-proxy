#!/bin/bash
set -e

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

# Start sample Java application as testuser
su - testuser -c "nohup java -jar /opt/math-game.jar > /dev/null 2>&1 &"

# Verify Java process started
sleep 2
if pgrep -f math-game >/dev/null; then
    echo "math-game.jar started successfully"
else
    echo "WARNING: math-game.jar may not have started"
fi

# Keep container alive
tail -f /dev/null
