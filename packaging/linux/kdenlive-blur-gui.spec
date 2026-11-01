Name:           kdenlive-blur-gui
Version:        1.0.0
Release:        1%{?dist}
Summary:        Detect, track and blur people and vehicles in Kdenlive projects
License:        GPL-3.0-or-later
URL:            https://github.com/YOUR-USER/kdenlive-blur-gui
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  desktop-file-utils
Requires:       python3 >= 3.10
Requires:       python3-tkinter
# Any ffmpeg provides this; ffmpeg-free (Fedora) is enough, RPM Fusion's ffmpeg also works.
Recommends:     /usr/bin/ffmpeg
Recommends:     kdenlive
Suggests:       intel-compute-runtime

%description
Opens an already cut Kdenlive project, finds people and vehicles in the parts of
the video that are really used on the timeline (YOLO), and writes a copy of the
project with tracking blur effects added. A check video can also be rendered
directly.

The Python components (PyTorch, Ultralytics, OpenCV) are deliberately NOT taken
from distribution packages: Fedora's OpenCV cannot decode H.264/HEVC footage
(for example from action cameras). On first start the tool offers to download
the components into a private folder in the user's home directory.

%prep
%autosetup

%build
# nothing to build

%install
install -Dpm 0755 packaging/linux/kdenlive-blur-gui %{buildroot}%{_bindir}/kdenlive-blur-gui
install -Dpm 0644 kdenlive_blur_gui.py %{buildroot}%{_datadir}/%{name}/kdenlive_blur_gui.py
install -Dpm 0644 kdenlive-blur-gui.png %{buildroot}%{_datadir}/%{name}/kdenlive-blur-gui.png
for size in 512 256 128 64 48; do
    install -Dpm 0644 assets/icons/${size}.png \
        %{buildroot}%{_datadir}/icons/hicolor/${size}x${size}/apps/kdenlive-blur-gui.png
done
desktop-file-install --dir=%{buildroot}%{_datadir}/applications packaging/linux/kdenlive-blur-gui.desktop

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/kdenlive-blur-gui.desktop

%files
%license LICENSE
%doc README.md
%{_bindir}/kdenlive-blur-gui
%{_datadir}/%{name}/
%{_datadir}/applications/kdenlive-blur-gui.desktop
%{_datadir}/icons/hicolor/*/apps/kdenlive-blur-gui.png

%changelog
* Sun Nov 01 2026 Your Name <you@example.com> - 1.0.0-1
- Initial package
