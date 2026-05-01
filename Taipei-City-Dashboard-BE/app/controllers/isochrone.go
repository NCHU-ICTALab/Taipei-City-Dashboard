package controllers

import (
	"TaipeiCityDashboardBE/app/cache"
	"TaipeiCityDashboardBE/global"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
)

// GetIsochrone proxies the Mapbox Isochrone API and caches the response in Redis.
// Query params: lng, lat, profile (default: driving-traffic), minutes (default: 15,30,45,60), colors
func GetIsochrone(c *gin.Context) {
	lng := c.Query("lng")
	lat := c.Query("lat")
	profile := c.DefaultQuery("profile", "driving-traffic")
	minutes := c.DefaultQuery("minutes", "15,30,45,60")
	colors := c.DefaultQuery("colors", "2ecc71,f1c40f,e67e22,e74c3c")

	if lng == "" || lat == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "lng and lat are required"})
		return
	}

	if global.MapboxToken == "" {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Mapbox token not configured"})
		return
	}

	cacheKey := fmt.Sprintf("isochrone:%s:%s:%s:%s", profile, lng, lat, minutes)

	// Return cached response if available
	if cached, err := cache.Redis.Get(cacheKey).Bytes(); err == nil {
		c.Data(http.StatusOK, "application/json", cached)
		return
	}

	apiURL := fmt.Sprintf(
		"https://api.mapbox.com/isochrone/v1/mapbox/%s/%s,%s?contours_minutes=%s&contours_colors=%s&polygons=true&generalize=50&denoise=1&access_token=%s",
		profile, lng, lat, minutes, colors, global.MapboxToken,
	)

	req, err := http.NewRequestWithContext(c.Request.Context(), http.MethodGet, apiURL, nil)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	if resp.StatusCode == http.StatusOK {
		// driving-traffic changes with congestion; cache for shorter time
		ttl := 60 * time.Minute
		if profile == "driving-traffic" {
			ttl = 10 * time.Minute
		}
		cache.Redis.Set(cacheKey, body, ttl)
	}

	c.Data(resp.StatusCode, "application/json", body)
}
