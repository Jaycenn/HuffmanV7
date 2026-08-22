psql --username=freecodecamp --dbname=postgres

-- Create the database
CREATE DATABASE universe;

-- Connect to the 'universe' database
\c universe;

-- Create the galaxy table
CREATE TABLE galaxy (
  galaxy_id SERIAL PRIMARY KEY,
  name VARCHAR(100) UNIQUE NOT NULL,
  galaxy_type TEXT NOT NULL,
  distance_from_earth NUMERIC NOT NULL,
  has_life BOOLEAN NOT NULL,
  is_spherical BOOLEAN NOT NULL
);

-- Create the star table
CREATE TABLE star (
  star_id SERIAL PRIMARY KEY,
  name VARCHAR(100) UNIQUE NOT NULL,
  galaxy_id INT REFERENCES galaxy(galaxy_id) NOT NULL,
  mass INT NOT NULL,
  age_in_millions_of_years INT NOT NULL,
  is_spherical BOOLEAN NOT NULL
);

-- Create the planet table
CREATE TABLE planet (
  planet_id SERIAL PRIMARY KEY,
  name VARCHAR(100) UNIQUE NOT NULL,
  star_id INT REFERENCES star(star_id) NOT NULL,
  has_life BOOLEAN NOT NULL,
  distance_from_star NUMERIC NOT NULL,
  planet_type TEXT NOT NULL
);

-- Create the moon table
CREATE TABLE moon (
  moon_id SERIAL PRIMARY KEY,
  name VARCHAR(100) UNIQUE NOT NULL,
  planet_id INT REFERENCES planet(planet_id) NOT NULL,
  diameter INT NOT NULL,
  has_atmosphere BOOLEAN NOT NULL,
  moon_type TEXT NOT NULL
);

-- Create the asteroid table
CREATE TABLE asteroid (
  asteroid_id SERIAL PRIMARY KEY,
  name VARCHAR(100) UNIQUE NOT NULL,
  star_id INT REFERENCES star(star_id) NOT NULL,
  diameter_km NUMERIC NOT NULL,
  is_potentially_hazardous BOOLEAN NOT NULL
);

-- Insert data into galaxy table
INSERT INTO galaxy (name, galaxy_type, distance_from_earth, has_life, is_spherical)
VALUES 
('Milky Way', 'Spiral', 0, TRUE, TRUE),
('Andromeda', 'Spiral', 2537000, FALSE, TRUE),
('Triangulum', 'Spiral', 3000000, FALSE, TRUE),
('Whirlpool', 'Spiral', 23000000, FALSE, TRUE),
('Sombrero', 'Elliptical', 29000000, FALSE, TRUE),
('Messier 87', 'Elliptical', 54000000, FALSE, TRUE);

-- Insert data into star table
INSERT INTO star (name, galaxy_id, mass, age_in_millions_of_years, is_spherical)
VALUES 
('Sun', 1, 1989000, 4600, TRUE),
('Proxima Centauri', 1, 123000, 4700, TRUE),
('Betelgeuse', 1, 16000000, 8000, TRUE),
('Vega', 1, 2100000, 455, TRUE),
('Sirius', 1, 2050000, 200, TRUE),
('Rigel', 1, 21000000, 9000, TRUE);

-- Insert data into planet table
INSERT INTO planet (name, star_id, has_life, distance_from_star, planet_type)
VALUES 
('Earth', 1, TRUE, 1.0, 'Terrestrial'),
('Mars', 1, FALSE, 1.52, 'Terrestrial'),
('Venus', 1, FALSE, 0.72, 'Terrestrial'),
('Mercury', 1, FALSE, 0.39, 'Terrestrial'),
('Jupiter', 1, FALSE, 5.2, 'Gas Giant'),
('Saturn', 1, FALSE, 9.58, 'Gas Giant'),
('Neptune', 1, FALSE, 30.07, 'Ice Giant'),
('Uranus', 1, FALSE, 19.22, 'Ice Giant'),
('Kepler-22b', 2, FALSE, 5.8, 'Exoplanet'),
('Gliese 581c', 2, FALSE, 0.073, 'Exoplanet'),
('HD 209458 b', 3, FALSE, 0.047, 'Hot Jupiter'),
('TOI-700 d', 4, FALSE, 0.163, 'Exoplanet');

-- Insert data into moon table
INSERT INTO moon (name, planet_id, diameter, has_atmosphere, moon_type)
VALUES 
('Moon', 1, 3474, FALSE, 'Rocky'),
('Phobos', 2, 22, FALSE, 'Rocky'),
('Deimos', 2, 12, FALSE, 'Rocky'),
('Io', 5, 3643, TRUE, 'Volcanic'),
('Europa', 5, 3121, TRUE, 'Icy'),
('Ganymede', 5, 5262, TRUE, 'Icy'),
('Callisto', 5, 4821, FALSE, 'Icy'),
('Titan', 6, 5150, TRUE, 'Icy'),
('Enceladus', 6, 504, TRUE, 'Icy'),
('Mimas', 6, 396, FALSE, 'Rocky'),
('Triton', 7, 2706, TRUE, 'Icy'),
('Miranda', 8, 471, FALSE, 'Rocky'),
('Ariel', 8, 1157, TRUE, 'Icy'),
('Umbriel', 8, 1169, FALSE, 'Icy'),
('Titania', 8, 1577, FALSE, 'Icy'),
('Oberon', 8, 1523, FALSE, 'Icy'),
('Kepler-22b I', 9, 3400, FALSE, 'Rocky'),
('Gliese 581c I', 10, 1800, FALSE, 'Rocky'),
('HD 209458 b I', 11, 3200, FALSE, 'Gaseous'),
('TOI-700 d I', 12, 2100, FALSE, 'Rocky');

-- Insert data into asteroid table
INSERT INTO asteroid (name, star_id, diameter_km, is_potentially_hazardous)
VALUES 
('Ceres', 1, 939.4, FALSE),
('Vesta', 1, 525.4, FALSE),
('Eros', 1, 16.8, TRUE),
('Apophis', 1, 0.37, TRUE),
('Bennu', 1, 0.49, TRUE),
('Hygiea', 1, 430, FALSE);
