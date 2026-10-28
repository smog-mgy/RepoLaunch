# Controlled repair acceptance fixture

This bundled Python HTTP sample deliberately imports requests without installing
it in the initial test recipe. The acceptance runner observes the real container
failure, asks the configured model for an allowed dependency repair, then rebuilds
and checks the JSON health response. It does not make outbound requests.
