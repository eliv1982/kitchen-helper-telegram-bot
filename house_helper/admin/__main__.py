"""`python -m house_helper.admin <command>`."""

from house_helper.admin.cli import main

if __name__ == "__main__":  # importing this module (the boundary test does) runs nothing
    raise SystemExit(main())
