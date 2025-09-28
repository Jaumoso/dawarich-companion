#!/usr/bin/env python3
"""
Dawarich Route Editor - API service for manually adding points to existing routes
"""

import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
import math
from typing import List, Dict, Any, Optional
import json

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

class RouteEditor:
    def __init__(self):
        self.db_config = {
            'host': os.getenv('DB_HOST', 'localhost'),
            'port': int(os.getenv('DB_PORT', '5432')),
            'database': os.getenv('DB_NAME', 'dawarich'),
            'user': os.getenv('DB_USER', 'dawarich'),
            'password': os.getenv('DB_PASSWORD', '')
        }

    def connect_db(self):
        """Connect to PostgreSQL database"""
        try:
            conn = psycopg2.connect(**self.db_config)
            return conn
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            raise

    def get_users(self) -> List[Dict]:
        """Get list of users for route selection"""
        conn = self.connect_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT DISTINCT id, email, 
                           COALESCE(first_name || ' ' || last_name, email) as display_name
                    FROM users 
                    WHERE id IN (SELECT DISTINCT user_id FROM points)
                    ORDER BY display_name
                """)
                return cur.fetchall()
        finally:
            conn.close()

    def get_user_routes(self, user_id: int, days_back: int = 30) -> List[Dict]:
        """Get routes for a specific user"""
        conn = self.connect_db()
        try:
            cutoff_date = datetime.now() - timedelta(days=days_back)
            
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT 
                        DATE(recorded_at) as route_date,
                        COUNT(*) as point_count,
                        MIN(recorded_at) as start_time,
                        MAX(recorded_at) as end_time,
                        MIN(latitude) as min_lat,
                        MAX(latitude) as max_lat,
                        MIN(longitude) as min_lon,
                        MAX(longitude) as max_lon
                    FROM points 
                    WHERE user_id = %s 
                        AND recorded_at >= %s
                        AND latitude IS NOT NULL 
                        AND longitude IS NOT NULL
                    GROUP BY DATE(recorded_at)
                    HAVING COUNT(*) >= 5
                    ORDER BY route_date DESC
                """, (user_id, cutoff_date))
                return cur.fetchall()
        finally:
            conn.close()

    def get_route_points(self, user_id: int, route_date: str) -> List[Dict]:
        """Get all points for a specific route"""
        conn = self.connect_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, latitude, longitude, recorded_at, accuracy, altitude, speed, battery
                    FROM points 
                    WHERE user_id = %s 
                        AND DATE(recorded_at) = %s
                        AND latitude IS NOT NULL 
                        AND longitude IS NOT NULL
                    ORDER BY recorded_at ASC
                """, (user_id, route_date))
                
                points = cur.fetchall()
                # Convert datetime objects to ISO strings for JSON serialization
                for point in points:
                    point['recorded_at'] = point['recorded_at'].isoformat()
                
                return points
        finally:
            conn.close()

    def find_insertion_timestamp(self, user_id: int, route_date: str, 
                                latitude: float, longitude: float) -> datetime:
        """Find the best timestamp for inserting a new point based on location"""
        points = self.get_route_points(user_id, route_date)
        
        if len(points) < 2:
            # If there are very few points, insert at the end
            return datetime.fromisoformat(points[-1]['recorded_at'].replace('Z', '+00:00')) + timedelta(seconds=30)
        
        best_position = 0
        min_distance_sum = float('inf')
        
        # Find the best position by minimizing total distance
        for i in range(len(points) - 1):
            point1 = points[i]
            point2 = points[i + 1]
            
            # Calculate distances from new point to both adjacent points
            dist1 = self.haversine_distance(
                latitude, longitude,
                point1['latitude'], point1['longitude']
            )
            dist2 = self.haversine_distance(
                latitude, longitude,
                point2['latitude'], point2['longitude']
            )
            
            # Check if this position makes sense (new point is roughly between the two)
            original_dist = self.haversine_distance(
                point1['latitude'], point1['longitude'],
                point2['latitude'], point2['longitude']
            )
            
            # If the sum of distances from new point to both adjacent points
            # is not much larger than the original distance, it's a good spot
            distance_penalty = dist1 + dist2 - original_dist
            
            if distance_penalty < min_distance_sum:
                min_distance_sum = distance_penalty
                best_position = i
        
        # Insert timestamp between the best position and next point
        point1 = points[best_position]
        point2 = points[best_position + 1]
        
        time1 = datetime.fromisoformat(point1['recorded_at'].replace('Z', '+00:00'))
        time2 = datetime.fromisoformat(point2['recorded_at'].replace('Z', '+00:00'))
        
        # Insert at midpoint time-wise
        time_diff = time2 - time1
        new_timestamp = time1 + time_diff / 2
        
        return new_timestamp

    def haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate the great circle distance between two points on earth in meters"""
        R = 6371000  # Earth's radius in meters
        
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = (math.sin(delta_lat / 2) * math.sin(delta_lat / 2) +
             math.cos(lat1_rad) * math.cos(lat2_rad) *
             math.sin(delta_lon / 2) * math.sin(delta_lon / 2))
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        return R * c

    def add_point_to_route(self, user_id: int, route_date: str, 
                          latitude: float, longitude: float,
                          altitude: Optional[float] = None,
                          accuracy: Optional[float] = None) -> Dict:
        """Add a new point to an existing route"""
        
        # Find the best timestamp for insertion
        timestamp = self.find_insertion_timestamp(user_id, route_date, latitude, longitude)
        
        conn = self.connect_db()
        try:
            with conn.cursor() as cur:
                # Check if a point already exists at this exact timestamp
                cur.execute("""
                    SELECT id FROM points 
                    WHERE user_id = %s AND recorded_at = %s
                """, (user_id, timestamp))
                
                if cur.fetchone():
                    # Adjust timestamp by a few seconds if conflict exists
                    timestamp += timedelta(seconds=5)
                
                # Insert the new point
                cur.execute("""
                    INSERT INTO points (
                        user_id, latitude, longitude, recorded_at, 
                        accuracy, altitude, speed, battery,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW()
                    ) RETURNING id
                """, (
                    user_id, latitude, longitude, timestamp,
                    accuracy or 20.0,  # Mark as manually added with moderate accuracy
                    altitude,
                    None,  # No speed for manually added points
                    None   # No battery data
                ))
                
                point_id = cur.fetchone()[0]
                conn.commit()
                
                logger.info(f"Added manual point {point_id} for user {user_id} at {timestamp}")
                
                return {
                    'success': True,
                    'point_id': point_id,
                    'timestamp': timestamp.isoformat(),
                    'message': 'Point added successfully'
                }
                
        except Exception as e:
            conn.rollback()
            logger.error(f"Error adding point: {e}")
            return {
                'success': False,
                'error': str(e)
            }
        finally:
            conn.close()

    def delete_point(self, user_id: int, point_id: int) -> Dict:
        """Delete a specific point (useful for removing mistakes)"""
        conn = self.connect_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM points 
                    WHERE id = %s AND user_id = %s
                    RETURNING id
                """, (point_id, user_id))
                
                deleted_point = cur.fetchone()
                
                if deleted_point:
                    conn.commit()
                    logger.info(f"Deleted point {point_id} for user {user_id}")
                    return {
                        'success': True,
                        'message': 'Point deleted successfully'
                    }
                else:
                    return {
                        'success': False,
                        'error': 'Point not found or not authorized'
                    }
                    
        except Exception as e:
            conn.rollback()
            logger.error(f"Error deleting point: {e}")
            return {
                'success': False,
                'error': str(e)
            }
        finally:
            conn.close()

# Initialize route editor
route_editor = RouteEditor()

# API Routes
@app.route('/api/users')
def get_users():
    """Get list of users"""
    try:
        users = route_editor.get_users()
        return jsonify(users)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/users/<int:user_id>/routes')
def get_user_routes(user_id):
    """Get routes for a user"""
    try:
        days_back = request.args.get('days', 30, type=int)
        routes = route_editor.get_user_routes(user_id, days_back)
        return jsonify(routes)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/users/<int:user_id>/routes/<route_date>/points')
def get_route_points(user_id, route_date):
    """Get points for a specific route"""
    try:
        points = route_editor.get_route_points(user_id, route_date)
        return jsonify(points)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/users/<int:user_id>/routes/<route_date>/points', methods=['POST'])
def add_point(user_id, route_date):
    """Add a new point to a route"""
    try:
        data = request.get_json()
        
        if not data or 'latitude' not in data or 'longitude' not in data:
            return jsonify({'error': 'Latitude and longitude are required'}), 400
        
        result = route_editor.add_point_to_route(
            user_id=user_id,
            route_date=route_date,
            latitude=float(data['latitude']),
            longitude=float(data['longitude']),
            altitude=data.get('altitude'),
            accuracy=data.get('accuracy')
        )
        
        if result['success']:
            return jsonify(result), 201
        else:
            return jsonify(result), 400
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/users/<int:user_id>/points/<int:point_id>', methods=['DELETE'])
def delete_point(user_id, point_id):
    """Delete a specific point"""
    try:
        result = route_editor.delete_point(user_id, point_id)
        
        if result['success']:
            return jsonify(result), 200
        else:
            return jsonify(result), 400
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health_check():
    """Health check endpoint"""
    try:
        conn = route_editor.connect_db()
        conn.close()
        return jsonify({'status': 'healthy'}), 200
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 503

@app.route('/')
def index():
    """Serve a simple web interface"""
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Dawarich Route Editor</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <style>
        body { margin: 0; font-family: Arial, sans-serif; }
        .container { display: flex; height: 100vh; }
        .sidebar { width: 300px; padding: 20px; background: #f5f5f5; overflow-y: auto; }
        .map-container { flex: 1; }
        #map { height: 100%; }
        .route-item { padding: 10px; margin: 5px 0; background: white; border-radius: 5px; cursor: pointer; }
        .route-item:hover { background: #e0e0e0; }
        .route-item.active { background: #007cba; color: white; }
        select, button { width: 100%; padding: 8px; margin: 5px 0; }
        .status { padding: 10px; margin: 10px 0; border-radius: 5px; }
        .status.success { background: #d4edda; color: #155724; }
        .status.error { background: #f8d7da; color: #721c24; }
        .instructions { margin: 10px 0; padding: 10px; background: #fff3cd; border-radius: 5px; font-size: 14px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="sidebar">
            <h2>Route Editor</h2>
            
            <div class="instructions">
                <strong>Instructions:</strong><br>
                1. Select a user<br>
                2. Choose a route<br>
                3. Click on map to add points<br>
                4. Right-click points to delete
            </div>
            
            <div>
                <label>User:</label>
                <select id="userSelect">
                    <option value="">Select a user...</option>
                </select>
            </div>
            
            <div>
                <label>Route:</label>
                <select id="routeSelect" disabled>
                    <option value="">Select a route...</option>
                </select>
            </div>
            
            <div id="status"></div>
            
            <div id="routeInfo" style="display: none;">
                <h3>Route Info</h3>
                <div id="routeDetails"></div>
                <button onclick="refreshRoute()">Refresh Points</button>
            </div>
        </div>
        
        <div class="map-container">
            <div id="map"></div>
        </div>
    </div>

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
        let map, routeLayer, currentUserId, currentRoute;
        
        // Initialize map
        map = L.map('map').setView([40.7128, -74.0060], 10);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap contributors'
        }).addTo(map);
        
        routeLayer = L.layerGroup().addTo(map);
        
        // Load users on page load
        loadUsers();
        
        function showStatus(message, type = 'success') {
            const statusDiv = document.getElementById('status');
            statusDiv.className = `status ${type}`;
            statusDiv.textContent = message;
            setTimeout(() => statusDiv.textContent = '', 3000);
        }
        
        async function loadUsers() {
            try {
                const response = await fetch('/api/users');
                const users = await response.json();
                const select = document.getElementById('userSelect');
                select.innerHTML = '<option value="">Select a user...</option>';
                users.forEach(user => {
                    const option = document.createElement('option');
                    option.value = user.id;
                    option.textContent = user.display_name;
                    select.appendChild(option);
                });
            } catch (error) {
                showStatus('Error loading users: ' + error.message, 'error');
            }
        }
        
        async function loadRoutes(userId) {
            try {
                const response = await fetch(`/api/users/${userId}/routes`);
                const routes = await response.json();
                const select = document.getElementById('routeSelect');
                select.innerHTML = '<option value="">Select a route...</option>';
                select.disabled = false;
                
                routes.forEach(route => {
                    const option = document.createElement('option');
                    option.value = route.route_date;
                    option.textContent = `${route.route_date} (${route.point_count} points)`;
                    select.appendChild(option);
                });
            } catch (error) {
                showStatus('Error loading routes: ' + error.message, 'error');
            }
        }
        
        async function loadRoute(userId, routeDate) {
            try {
                const response = await fetch(`/api/users/${userId}/routes/${routeDate}/points`);
                const points = await response.json();
                
                routeLayer.clearLayers();
                
                if (points.length === 0) {
                    showStatus('No points found for this route', 'error');
                    return;
                }
                
                // Create route line
                const latLngs = points.map(point => [point.latitude, point.longitude]);
                const routeLine = L.polyline(latLngs, { color: 'blue', weight: 3 }).addTo(routeLayer);
                
                // Add point markers
                points.forEach(point => {
                    const marker = L.circleMarker([point.latitude, point.longitude], {
                        radius: 5,
                        color: 'red',
                        fillColor: 'red',
                        fillOpacity: 0.8
                    }).addTo(routeLayer);
                    
                    marker.bindPopup(`
                        <strong>Point ${point.id}</strong><br>
                        Time: ${new Date(point.recorded_at).toLocaleString()}<br>
                        Accuracy: ${point.accuracy || 'N/A'}m<br>
                        <button onclick="deletePoint(${point.id})">Delete</button>
                    `);
                });
                
                // Fit map to route
                map.fitBounds(routeLine.getBounds(), { padding: [20, 20] });
                
                // Update route info
                document.getElementById('routeInfo').style.display = 'block';
                document.getElementById('routeDetails').innerHTML = `
                    <p><strong>Date:</strong> ${routeDate}</p>
                    <p><strong>Points:</strong> ${points.length}</p>
                `;
                
                showStatus(`Loaded ${points.length} points`);
                
            } catch (error) {
                showStatus('Error loading route: ' + error.message, 'error');
            }
        }
        
        async function addPoint(lat, lng) {
            if (!currentUserId || !currentRoute) {
                showStatus('Please select a user and route first', 'error');
                return;
            }
            
            try {
                const response = await fetch(`/api/users/${currentUserId}/routes/${currentRoute}/points`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        latitude: lat,
                        longitude: lng,
                        accuracy: 20
                    })
                });
                
                const result = await response.json();
                
                if (result.success) {
                    showStatus('Point added successfully');
                    loadRoute(currentUserId, currentRoute); // Refresh the route
                } else {
                    showStatus('Error adding point: ' + result.error, 'error');
                }
                
            } catch (error) {
                showStatus('Error adding point: ' + error.message, 'error');
            }
        }
        
        async function deletePoint(pointId) {
            if (!currentUserId) return;
            
            try {
                const response = await fetch(`/api/users/${currentUserId}/points/${pointId}`, {
                    method: 'DELETE'
                });
                
                const result = await response.json();
                
                if (result.success) {
                    showStatus('Point deleted successfully');
                    loadRoute(currentUserId, currentRoute); // Refresh the route
                } else {
                    showStatus('Error deleting point: ' + result.error, 'error');
                }
                
            } catch (error) {
                showStatus('Error deleting point: ' + error.message, 'error');
            }
        }
        
        function refreshRoute() {
            if (currentUserId && currentRoute) {
                loadRoute(currentUserId, currentRoute);
            }
        }
        
        // Event listeners
        document.getElementById('userSelect').addEventListener('change', function() {
            currentUserId = this.value;
            document.getElementById('routeSelect').innerHTML = '<option value="">Select a route...</option>';
            document.getElementById('routeSelect').disabled = true;
            document.getElementById('routeInfo').style.display = 'none';
            routeLayer.clearLayers();
            
            if (currentUserId) {
                loadRoutes(currentUserId);
            }
        });
        
        document.getElementById('routeSelect').addEventListener('change', function() {
            currentRoute = this.value;
            if (currentUserId && currentRoute) {
                loadRoute(currentUserId, currentRoute);
            }
        });
        
        // Map click to add points
        map.on('click', function(e) {
            addPoint(e.latlng.lat, e.latlng.lng);
        });
        
        // Show coordinates on hover
        map.on('mousemove', function(e) {
            // You could add coordinate display here if desired
        });
    </script>
</body>
</html>
    """)

if __name__ == "__main__":
    port = int(os.getenv('PORT', '5000'))
    debug = os.getenv('DEBUG', 'false').lower() == 'true'
    
    logger.info(f"Starting Dawarich Route Editor on port {port}")
    app.run(host='0.0.0.0', port=port, debug=debug)