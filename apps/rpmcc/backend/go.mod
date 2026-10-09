// The app-engine wrapper for RPM Command Center.
//
// The replace points at a sibling checkout of rpm-command-center so the app
// always builds against the working tree you are editing rather than a
// published tag. Adjust the path if your checkout lives elsewhere.
module app.local/rpmcc

go 1.26.2

require github.com/servicenow/rpm-command-center v0.0.0

require (
	github.com/creack/pty/v2 v2.0.1 // indirect
	github.com/fatih/color v1.19.0 // indirect
	github.com/mattn/go-colorable v0.1.14 // indirect
	github.com/mattn/go-isatty v0.0.20 // indirect
	golang.org/x/sys v0.47.0 // indirect
	golang.org/x/term v0.45.0 // indirect
	gopkg.in/yaml.v3 v3.0.1 // indirect
)

replace github.com/servicenow/rpm-command-center => ../../../../rpm-command-center
