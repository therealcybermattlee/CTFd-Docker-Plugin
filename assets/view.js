CTFd._internal.challenge.data = undefined;

function _container_get_renderer() {
	var md = CTFd && CTFd.lib && CTFd.lib.markdown;
	if (typeof md === "function") {
		// Older API: CTFd.lib.markdown() returns a renderer
		try { return md(); } catch (e) { /* fall through */ }
	}
	if (md && typeof md.render === "function") {
		// Newer API: CTFd.lib.markdown IS the renderer
		return md;
	}
	// Fallback: escape text and wrap in <p>
	return {
		render: function (text) {
			var div = document.createElement("div");
			div.textContent = text == null ? "" : String(text);
			return "<p>" + div.innerHTML + "</p>";
		},
	};
}

CTFd._internal.challenge.preRender = function () {
	CTFd._internal.challenge.renderer = _container_get_renderer();
};

CTFd._internal.challenge.render = function (markdown) {
	if (!CTFd._internal.challenge.renderer) {
		CTFd._internal.challenge.renderer = _container_get_renderer();
	}
	return CTFd._internal.challenge.renderer.render(markdown);
};

function _container_inject_controls() {
	var modal = document.getElementById("challenge-window") || document.querySelector('[role="dialog"]');
	if (!modal) return;
	if (modal.querySelector("#container-request-btn") || modal.querySelector("#container-request-result")) {
		return; // already injected
	}
	var descSpan = modal.querySelector(".challenge-desc");
	if (!descSpan) return;

	var idInput = modal.querySelector("#challenge-id");
	var challengeId = idInput ? parseInt(idInput.value, 10) : NaN;
	if (!challengeId) return;

	var wrap = document.createElement("div");
	wrap.className = "container-challenge-controls text-center my-3";
	wrap.innerHTML = [
		'<button type="button" class="btn btn-success" id="container-request-btn">Get Connection Info</button>',
		'<div id="container-request-result" style="display: none;">',
		'  <p><code id="container-connection-info"></code></p>',
		'  <p>Expires in <span id="container-expires"></span> minutes (<span id="container-expires-time"></span>)</p>',
		'  <p>',
		'    <button type="button" class="btn btn-info" id="container-reset-btn">Reset</button>',
		'    <button type="button" class="btn btn-info" id="container-stop-btn">Stop</button>',
		'    <button type="button" class="btn btn-info" id="container-renew-btn">Add Time</button>',
		'  </p>',
		'</div>',
		'<div id="container-request-error" class="alert alert-danger mt-2" role="alert" style="display: none;">',
		'  <strong id="result-message">Error</strong>',
		'</div>',
	].join("\n");
	descSpan.parentNode.insertBefore(wrap, descSpan.nextSibling);

	wrap.querySelector("#container-request-btn").addEventListener("click", function () {
		container_request(challengeId);
	});
	wrap.querySelector("#container-reset-btn").addEventListener("click", function () {
		container_reset(challengeId);
	});
	wrap.querySelector("#container-stop-btn").addEventListener("click", function () {
		container_stop(challengeId);
	});
	wrap.querySelector("#container-renew-btn").addEventListener("click", function () {
		container_renew(challengeId);
	});
}

CTFd._internal.challenge.postRender = function () {
	// Defer one tick so Alpine has finished swapping in $store.challenge.data.view
	setTimeout(_container_inject_controls, 0);
};

CTFd._internal.challenge.submit = function (preview) {
	var challenge_id = parseInt(CTFd.lib.$("#challenge-id").val());
	var submission = CTFd.lib.$("#challenge-input").val();

	var body = {
		challenge_id: challenge_id,
		submission: submission,
	};
	var params = {};
	if (preview) {
		params["preview"] = true;
	}

	return CTFd.api
		.post_challenge_attempt(params, body)
		.then(function (response) {
			if (response.status === 429) {
				// User was ratelimited but process response
				return response;
			}
			if (response.status === 403) {
				// User is not logged in or CTF is paused.
				return response;
			}
			return response;
		});
};

function mergeQueryParams(parameters, queryParameters) {
	if (parameters.$queryParameters) {
		Object.keys(parameters.$queryParameters).forEach(function (
			parameterName
		) {
			var parameter = parameters.$queryParameters[parameterName];
			queryParameters[parameterName] = parameter;
		});
	}

	return queryParameters;
}

function _container_show_error(msg) {
	var requestError = document.getElementById("container-request-error");
	requestError.style.display = "";
	requestError.firstElementChild.innerHTML = msg;
}

function _container_restore_button(button, originalLabel) {
	if (!button) return;
	button.innerHTML = originalLabel;
	button.removeAttribute("disabled");
}

function _container_show_running(data, opts) {
	opts = opts || {};
	var connectionInfo = document.getElementById("container-connection-info");
	var requestResult = document.getElementById("container-request-result");
	var containerExpires = document.getElementById("container-expires");
	var containerExpiresTime = document.getElementById("container-expires-time");
	var requestButton = document.getElementById("container-request-btn");
	var requestError = document.getElementById("container-request-error");

	requestError.style.display = "none";
	requestError.firstElementChild.innerHTML = "";
	if (opts.removeRequestButton && requestButton && requestButton.parentNode) {
		requestButton.parentNode.removeChild(requestButton);
	}
	connectionInfo.innerHTML = data.hostname + ":" + data.port;
	containerExpires.innerHTML = Math.ceil(
		(new Date(data.expires * 1000) - new Date()) / 1000 / 60
	);
	containerExpiresTime.innerHTML = new Date(
		data.expires * 1000
	).toLocaleTimeString();
	requestResult.style.display = "";
}

function _container_poll_status(rowId, button, originalLabel, opts) {
	var attempts = 0;
	var maxAttempts = 60; // 60 * 3s = 3 minutes
	var timer = setInterval(function () {
		attempts++;
		if (attempts > maxAttempts) {
			clearInterval(timer);
			_container_show_error("Provisioning timed out after 3 minutes");
			_container_restore_button(button, originalLabel);
			return;
		}
		fetch("/containers/api/status/" + rowId, {
			method: "GET",
			headers: {
				"Accept": "application/json",
				"CSRF-Token": init.csrfNonce,
			},
			credentials: "same-origin",
		})
			.then(function (r) {
				return r.json();
			})
			.then(function (data) {
				if (data.status === "running") {
					clearInterval(timer);
					_container_show_running(data, opts);
					if (opts && opts.restoreButtonOnSuccess) {
						_container_restore_button(button, originalLabel);
					}
				} else if (data.status === "failed") {
					clearInterval(timer);
					_container_show_error(data.error || "Provisioning failed");
					_container_restore_button(button, originalLabel);
				}
				// else: still provisioning, keep polling
			})
			.catch(function (err) {
				clearInterval(timer);
				_container_show_error("Status check failed: " + err);
				_container_restore_button(button, originalLabel);
			});
	}, 3000);
}

function _container_async_action(path, challenge_id, button, opts) {
	var originalLabel = button.innerHTML;
	button.setAttribute("disabled", "disabled");
	button.innerHTML = "Provisioning…";

	fetch(path, {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			"Accept": "application/json",
			"CSRF-Token": init.csrfNonce,
		},
		credentials: "same-origin",
		body: JSON.stringify({ chal_id: challenge_id }),
	})
		.then(function (r) {
			return r.json();
		})
		.then(function (data) {
			if (data.error !== undefined) {
				_container_show_error(data.error);
				_container_restore_button(button, originalLabel);
				return;
			}
			if (data.message !== undefined) {
				_container_show_error(data.message);
				_container_restore_button(button, originalLabel);
				return;
			}
			if (data.status === "running") {
				_container_show_running(data, opts);
				if (opts && opts.restoreButtonOnSuccess) {
					_container_restore_button(button, originalLabel);
				}
				return;
			}
			if (data.status === "provisioning") {
				_container_poll_status(data.id, button, originalLabel, opts);
				return;
			}
			_container_show_error("Unexpected response: " + JSON.stringify(data));
			_container_restore_button(button, originalLabel);
		})
		.catch(function (err) {
			_container_show_error("Request failed: " + err);
			_container_restore_button(button, originalLabel);
		});
}

function container_request(challenge_id) {
	var requestButton = document.getElementById("container-request-btn");
	_container_async_action(
		"/containers/api/request",
		challenge_id,
		requestButton,
		{ removeRequestButton: true }
	);
}

function container_reset(challenge_id) {
	var resetButton = document.getElementById("container-reset-btn");
	_container_async_action(
		"/containers/api/reset",
		challenge_id,
		resetButton,
		{ restoreButtonOnSuccess: true }
	);
}

function container_renew(challenge_id) {
	var path = "/containers/api/renew";
	var renewButton = document.getElementById("container-renew-btn");
	var requestResult = document.getElementById("container-request-result");
	var containerExpires = document.getElementById("container-expires");
	var containerExpiresTime = document.getElementById(
		"container-expires-time"
	);
	var requestError = document.getElementById("container-request-error");

	renewButton.setAttribute("disabled", "disabled");

	var xhr = new XMLHttpRequest();
	xhr.open("POST", path, true);
	xhr.setRequestHeader("Content-Type", "application/json");
	xhr.setRequestHeader("Accept", "application/json");
	xhr.setRequestHeader("CSRF-Token", init.csrfNonce);
	xhr.send(JSON.stringify({ chal_id: challenge_id }));
	xhr.onload = function () {
		var data = JSON.parse(this.responseText);
		if (data.error !== undefined) {
			// Container rrror
			requestError.style.display = "";
			requestError.firstElementChild.innerHTML = data.error;
			renewButton.removeAttribute("disabled");
		} else if (data.message !== undefined) {
			// CTFd error
			requestError.style.display = "";
			requestError.firstElementChild.innerHTML = data.message;
			renewButton.removeAttribute("disabled");
		} else {
			// Success
			requestError.style.display = "none";
			requestResult.style.display = "";
			containerExpires.innerHTML = Math.ceil(
				(new Date(data.expires * 1000) - new Date()) / 1000 / 60
			);
			containerExpiresTime.innerHTML = new Date(
				data.expires * 1000
			).toLocaleTimeString();
			renewButton.removeAttribute("disabled");
		}
		console.log(data);
	};
}

function container_stop(challenge_id) {
	var path = "/containers/api/stop";
	var stopButton = document.getElementById("container-stop-btn");
	var requestResult = document.getElementById("container-request-result");
	var connectionInfo = document.getElementById("container-connection-info");
	var requestError = document.getElementById("container-request-error");

	stopButton.setAttribute("disabled", "disabled");

	var xhr = new XMLHttpRequest();
	xhr.open("POST", path, true);
	xhr.setRequestHeader("Content-Type", "application/json");
	xhr.setRequestHeader("Accept", "application/json");
	xhr.setRequestHeader("CSRF-Token", init.csrfNonce);
	xhr.send(JSON.stringify({ chal_id: challenge_id }));
	xhr.onload = function () {
		var data = JSON.parse(this.responseText);
		if (data.error !== undefined) {
			// Container rrror
			requestError.style.display = "";
			requestError.firstElementChild.innerHTML = data.error;
			stopButton.removeAttribute("disabled");
		} else if (data.message !== undefined) {
			// CTFd error
			requestError.style.display = "";
			requestError.firstElementChild.innerHTML = data.message;
			stopButton.removeAttribute("disabled");
		} else {
			// Success
			requestError.style.display = "none";
			requestResult.innerHTML =
				"Container stopped. Reopen this challenge to start another.";
		}
		console.log(data);
	};
}
