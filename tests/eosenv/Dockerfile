# The switch's software, for the off-box suite: Python 3.9 (EOS 4.28 has
# 3.9.17), bash 4.2 first on PATH (EOS has 4.2.46), util-linux flock and GNU
# coreutils, and an ordinary user. CI runs the suite in it as that user and as
# root; CONTRIBUTING.md says how to run it by hand.
FROM python:3.9-bookworm
# The SHA-256 of the bash-4.2.tar.gz whose .sig from ftp.gnu.org verifies
# against gnu-keyring.gpg: Chet Ramey, key 7C01 35FB 088A AF6C 66C6 50B9 BB58
# 69F0 64EA 74AB.
RUN curl -fsSL https://ftp.gnu.org/gnu/bash/bash-4.2.tar.gz -o /tmp/bash.tgz \
 && echo 'a27a1179ec9c0830c65c6aa5d7dab60f7ce1a2a608618570f96bfa72e95ab3d8  /tmp/bash.tgz' | sha256sum -c - \
 && tar -xzf /tmp/bash.tgz -C /tmp && cd /tmp/bash-4.2 \
 && CFLAGS="-O2 -std=gnu89 -Wno-implicit-function-declaration -Wno-int-conversion -Wno-incompatible-pointer-types" ./configure --prefix=/opt/bash42 --without-bash-malloc >/dev/null \
 && make -j4 >/dev/null 2>&1 && make install >/dev/null \
 && /opt/bash42/bin/bash --version | head -1 && rm -rf /tmp/bash*
RUN apt-get update -qq && apt-get install -y -qq util-linux >/dev/null && command -v flock
ENV PATH=/opt/bash42/bin:$PATH
RUN useradd -m tester
USER tester
