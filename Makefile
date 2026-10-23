.PHONY: test static validate integration

test:
	python3 -m unittest discover -s tests -v

static:
	python3 -m compileall -q dd_manager.py dojo tests tools

validate:
	python3 tools/validate.py

integration:
	DDM_INTEGRATION=1 python3 -m unittest discover -s tests -p test_integration.py -v
