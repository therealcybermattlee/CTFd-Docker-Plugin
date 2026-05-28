CTFd.plugin.run((_CTFd) => {
	const $ = _CTFd.lib.$;
	const md = _CTFd.lib.markdown();
});

(function () {
	var containerImage = document.getElementById("container-image");
	var containerImageDefault = document.getElementById("container-image-default");

	function setStatus(msg) {
		if (containerImageDefault) containerImageDefault.innerHTML = msg;
	}

	var xhr = new XMLHttpRequest();
	xhr.open("GET", "/containers/api/images", true);
	xhr.setRequestHeader("Accept", "application/json");
	xhr.setRequestHeader("CSRF-Token", init.csrfNonce);

	xhr.onload = function () {
		var data;
		try {
			data = JSON.parse(this.responseText);
		} catch (e) {
			setStatus("Could not load image list (HTTP " + this.status + ")");
			console.error("image list parse failed:", e, this.responseText);
			return;
		}
		if (data && data.error !== undefined) {
			setStatus("Image list error: " + data.error);
			console.error("image list error:", data.error);
			return;
		}
		var images = (data && data.images) || [];
		for (var i = 0; i < images.length; i++) {
			var opt = document.createElement("option");
			opt.value = images[i];
			opt.innerHTML = images[i];
			containerImage.appendChild(opt);
		}
		setStatus(images.length === 0 ? "(registry returned no images)" : "Choose an image…");
		containerImage.removeAttribute("disabled");
	};
	xhr.onerror = function () {
		setStatus("Could not reach /containers/api/images");
		console.error("image list xhr.onerror");
	};
	xhr.send();
})();
