# Homebrew formula for OpenFilmKit. Each release is copied into aidansiegel/homebrew-openfilmkit as
# Formula/openfilmkit.rb with the release tarball's sha256 filled in; this copy can't hold the
# checksum of the tarball it is part of, so it keeps a placeholder.
class Openfilmkit < Formula
  desc "Free film scanning and negative conversion from the command line, built on SANE"
  homepage "https://github.com/aidansiegel/OpenFilmKit"
  url "https://github.com/aidansiegel/OpenFilmKit/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "GPL-3.0-or-later"
  head "https://github.com/aidansiegel/OpenFilmKit.git", branch: "main"

  depends_on "autoconf" => :build
  depends_on "autoconf-archive" => :build
  depends_on "automake" => :build
  depends_on "gettext" => :build
  depends_on "libtool" => :build
  depends_on "pkgconf" => :build
  depends_on "jpeg-turbo"
  depends_on "libpng"
  depends_on "libtiff"
  depends_on "libusb"
  depends_on "python@3.14"

  # SANE at the commit pinned in packaging/sane-pin.json (keep the two in step); Homebrew checks
  # the revision. tools/build_sane.py applies patches/ and builds only the backends OpenFilmKit
  # ships, into this formula's own prefix, apart from Homebrew's sane-backends.
  resource "sane-backends" do
    url "https://gitlab.com/sane-project/backends.git",
        revision: "8ebb7c50e3ea4820cd8f62eccc4516e326ddbbb3"
  end

  def install
    python = Formula["python@3.14"].opt_bin/"python3.14"
    resource("sane-backends").stage buildpath/"sane-src"
    # Also regenerates openfilmkit/data from the same SANE tree before the package is installed.
    system python, "tools/build_sane.py", "--work", buildpath/"sane-work",
           "--source", buildpath/"sane-src", "--prefix", libexec/"sane", "--jobs", ENV.make_jobs.to_s

    # numpy, Pillow and OpenCV at the exact versions OpenFilmKit is tested with, checked by hash.
    venv = libexec/"venv"
    system python, "-m", "venv", venv
    system venv/"bin/pip", "install", "--no-cache-dir", "--only-binary=:all:", "--require-hashes",
           "-r", "packaging/requirements-brew.txt"
    system venv/"bin/pip", "install", "--no-cache-dir", "--no-deps", "--no-build-isolation", "."

    (bin/"openfilmkit").write_env_script venv/"bin/openfilmkit", OPENFILMKIT_RUNTIME: opt_libexec/"sane"
  end

  def caveats
    on_linux do
      <<~EOS
        On Linux, scanners need a USB permission rule before a normal user can open them:
          sudo install -m 644 #{opt_libexec}/sane/share/openfilmkit/60-openfilmkit.rules /etc/udev/rules.d/
          sudo udevadm control --reload && sudo udevadm trigger
        `openfilmkit doctor` checks for it.
      EOS
    end
  end

  test do
    # No scanner is connected during `brew test`, so doctor exits 2; the runtime must still report
    # the pinned SANE commit and its patches.
    output = shell_output("#{bin}/openfilmkit doctor --json", 2)
    assert_match "8ebb7c50", output
    assert_match "0001-gl128-afe-port-replay.diff", output
    assert_match "agfa-hdc200", shell_output("#{bin}/openfilmkit profiles")
  end
end
