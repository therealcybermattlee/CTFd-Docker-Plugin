(function () {
	var containerImage = document.getElementById("container-image");
	var containerImageDefault = document.getElementById("container-image-default");

	function setStatus(msg) {
		if (containerImageDefault) containerImageDefault.innerHTML = msg;
	}
	function existingValues() {
		var vals = {};
		for (var i = 0; i < containerImage.options.length; i++) {
			vals[containerImage.options[i].value] = true;
		}
		return vals;
	}

	// The saved image is already rendered as a <option selected> in update.html, so
	// the form can submit even if this XHR fails. We just append more registry tags
	// when they arrive, and surface a useful status when they don't.
	var xhr = new XMLHttpRequest();
	xhr.open("GET", "/containers/api/images", true);
	xhr.setRequestHeader("Accept", "application/json");
	xhr.setRequestHeader("CSRF-Token", init.csrfNonce);

	xhr.onload = function () {
		var data;
		try {
			data = JSON.parse(this.responseText);
		} catch (e) {
			setStatus("Could not load image list (HTTP " + this.status + ") — saved image preserved");
			console.error("image list parse failed:", e, this.responseText);
			return;
		}
		if (data && data.error !== undefined) {
			setStatus("Image list error: " + data.error + " — saved image preserved");
			console.error("image list error:", data.error);
			return;
		}
		var existing = existingValues();
		var images = (data && data.images) || [];
		for (var i = 0; i < images.length; i++) {
			if (existing[images[i]]) continue;
			var opt = document.createElement("option");
			opt.value = images[i];
			opt.innerHTML = images[i];
			containerImage.appendChild(opt);
		}
		setStatus(images.length === 0 ? "(registry returned no images)" : "Choose an image…");
		// Re-assert the saved selection in case browsers reordered options.
		if (typeof container_image_selected !== "undefined" && container_image_selected) {
			containerImage.value = container_image_selected;
		}
	};
	xhr.onerror = function () {
		setStatus("Could not reach /containers/api/images — saved image preserved");
		console.error("image list xhr.onerror");
	};
	xhr.send();
})();
