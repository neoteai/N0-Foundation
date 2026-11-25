Assets are from https://danfergo.github.io/gelsight-simulation/

@article{9369877,
    title={Generation of GelSight Tactile Images for Sim2Real Learning},
    author={Daniel Fernandes Gomes and Paolo Paoletti and Shan Luo},
    journal={IEEE Robotics and Automation Letters},
    year={2021},
    volume={6},
    number={2},
    pages={4177-4184},
    doi={10.1109/LRA.2021.3063925},
}


-> STL files were imported into Isaac and turned into USD files.


The object base plates have a width and length of 16mm. The maximum height is 20mm.
We want to end up at the top of the shape, when we move to the object position.
This is why we set the xForm to be at the top.

Here is the cone asset as an example:
![cone_example_img](<Screenshot from 2025-07-25 12-10-33.png>)

Every asset is a rigid body with an SDF mesh.
