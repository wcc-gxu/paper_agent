IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

1 

## Tricking Autonomous Driving Systems via Adversarial Taillights 

_**Abstract**_ **—The occupancy network is widely used in autonomous driving systems (e.g., Tesla) and is vulnerable to adversarial attacks. In this paper, we are the first to reveal the collision vulnerability of the occupancy network under a novel physical-world attack vector: programmable vehicle taillights. We formulate the attack as an optimization problem to generate physically realizable adversarial taillights that mislead the occupancy network into failing to detect the adversarial vehicle, thereby causing a collision. To generate these adversarial taillights, we fine-tune a pre-trained generative adversarial network to steer its latent space toward the adversarial sample space of the occupancy network, guided by gradient feedback. Furthermore, to ensure the naturalness and robustness of the generated adversarial taillights, we incorporate color constraints, geometric transformations, and photometric augmentations into the training process. We extensively evaluate the effectiveness of this novel attack in both simulated and real-world environments. Experimental results demonstrate that our algorithm effectively generates adversarial taillights, achieving an average attack success rate of 93% and up to 100% in the best case, while maintaining robustness and stealthiness. We have successfully implemented our attacks on a real-world vehicle, triggering vehicle collisions.** 

_**Index Terms**_ **—Autonomous Driving Systems; Adversarial Attacks; Adversarial Taillights.** 

## I. INTRODUCTION 

Autonomous driving systems are increasingly integrated into contemporary vehicles produced by manufacturers such as Tesla, Audi, and BYD. However, recent real-world incidents and large-scale recalls have exposed significant vulnerabilities in these systems. For instance, Xiaomi Auto has recalled over 110,000 SU7 vehicles after its autonomous driving system caused three fatalities in a single incident [1]. Similarly, Tesla’s Full Self-Driving (FSD) system, which employs an occupancy network for camera-only perception [2], has exhibited repeated recognition failures in complex and safety-critical traffic scenarios, such as railway crossings and degraded visibility conditions, highlighting the severe limitations of pure vision-based perception algorithms in generalizing to real-world environments [3, 4]. These events underscore the urgent need to examine vulnerabilities in current autonomous driving systems. 

Environmental perception serves as the cornerstone of autonomous driving systems [5]. Among various sensor modalities, camera-only approaches have gained prominence due to their cost-effectiveness and robust semantic understanding capabilities [6]. As a next-generation paradigm for camera-only 3D perception, occupancy prediction overcomes the limitations of sparse 3D object detection and 2D bird’s-eye-view (BEV) methods by delivering a dense voxel 

grid representation of the surrounding environment [7–9]. This has driven its rapid adoption in production-grade autonomous driving systems, notably Tesla’s Full Self-Driving stack, which employs occupancy-based representations for comprehensive scene understanding [2, 10]. However, the vulnerabilities of 3D occupancy prediction have not been systematically investigated, representing a significant and underexplored research gap [11]. 

Adversarial attacks involve crafting input perturbations to induce incorrect outputs from machine learning models. State-of-the-art attack methods cannot be directly applied to occupancy networks due to two primary design challenges. First, prior studies on lane detection attacks [12–17] focus on altering the shape of detected traffic lanes, making it difficult to directly apply to object detection models [13]. Second, existing object detection attacks [18–21] target modifications to class or bounding box probabilities. However, occupancy networks represent objects as contiguous voxel grids sharing identical semantic classes, which fundamentally differs from conventional object detection paradigms. Consequently, optimization objective functions from prior work are inapplicable to this context. 

State-of-the-art (SOTA) attack vectors against autonomous driving systems include stickers [22], 3D-printed decoys [23, 24], and light or laser projections [25]. These vectors are readily detectable, as they introduce additional physical objects or anomalous light. Moreover, they are costly and unstable. Attackers are also easily exposed under surveillance, since they must personally deploy the physical elements. 

To address these limitations, we investigated vehicle architectures and occupancy networks, deriving two key observations. First, many modern vehicles, such as those from Huawei AITO and IM Motors (SAIC Motors), are equipped with programmable taillights. These manufacturers have cumulatively delivered over one million such vehicles [26]. As these vehicles are internet-connected, their programmable taillights can be remotely manipulated, providing a convenient and stealthy attack vector. Second, occupancy networks represent the driving scene as dense grids, with each grid cell comprising a small voxel (3D pixel). If a contiguous region of grids is classified as a drivable surface or non-occupied, and the region’s size exceeds that of the vehicle, autonomous driving systems deem the region passable. Consequently, jointly attacking a contiguous region of grids can induce a vehicle collision. 

In this paper, we are the first to expose the collision vulnerability in occupancy networks using a novel physical-world attack vector: programmable vehicle taillights. Specifically, we render a 3D vehicle model incorporating programmable taillights sourced from manufacturers and composite it with real-world driving-perspective images. We 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

2 

then jointly perturb the pixels in the contiguous taillight region to generate adversarial taillights. This process is formulated as an optimization problem aimed at misleading the occupancy network into failing to detect the vehicle equipped with the adversarial taillight, thereby inducing a collision. To optimize the adversarial taillights, we fine-tune a pre-trained generative adversarial network (GAN) to steer its latent space toward the adversarial sample space of the occupancy network, guided by gradient feedback. The GAN iteratively generates perturbed images, which are fed into the occupancy network; the resulting gradients are computed and used to update the GAN iteratively. Furthermore, to ensure the naturalness and robustness of the generated adversarial taillights, we incorporate color constraints, geometric transformations, and photometric augmentations into the training process. 

To demonstrate the effectiveness of the proposed method, we extensively evaluate this novel attack in both simulated and real-world environments. Experimental results show that our algorithm effectively generates adversarial taillights, achieving an average attack success rate of 93% and up to 100% in the best case, while maintaining robustness and stealthiness. Further, we successfully implemented our attacks on a real-world vehicle, triggering vehicle collisions. 

The main contributions of this work are summarized as follows: 

- We are the first to expose the collision vulnerability in occupancy networks using a novel physical-world attack vector: programmable vehicle taillights. 

- We formulate the attack as an optimization problem to generate physically realizable adversarial taillights that mislead the occupancy network into failing to detect the vehicle, thereby inducing a collision. To this end, we fine-tune a pre-trained GAN to steer its latent space toward the adversarial sample space of the occupancy network, guided by gradient feedback. 

- We extensively evaluate the effectiveness of this novel attack in both simulated and real-world environments. Experimental results demonstrate that our algorithm effectively generates adversarial taillights, achieving an average attack success rate of 93% (up to 100% in the best case) while maintaining robustness and stealthiness. Further, we successfully implemented our attacks on a real-world vehicle, triggering vehicle collisions. 

The remainder of this paper is organized as follows. Section II introduces the background. Section III details the proposed method. Section IV provides an evaluation of the proposed attack method. Section V presents real-world experiments. Section VI reviews related work, and Section VII concludes the paper. 

## II. BACKGROUND 

## _A. The occupancy network_ 

3D occupancy prediction has emerged as a next-generation perception paradigm for camera-only autonomous driving. Tesla has replaced its bird’s-eye-view (BEV)-based perception modules with an occupancy network that predicts a volumetric semantic field from surround-view camera inputs [2, 10]. 

**==> picture [253 x 114] intentionally omitted <==**

**----- Start of picture text -----**<br>
Predict<br>Muiti-camera Images 3D Occupancy Prediction<br>**----- End of picture text -----**<br>


Fig. 1: The prediction result of the occupancy network. Each class is represented by a distinct color: _vehicle_ in blue, _drivable surface_ in purple, and _non-occupied_ uncolored. 

This transition from BEV to occupancy prediction enables Tesla’s camera-only stack to reason continuously about surface geometry and occlusion, thereby enhancing robustness in cluttered, low-light, or adverse weather conditions. This shift reflects a broader industry trend toward unified grid-based perception frameworks that integrate obstacle detection and mapping. 

Occupancy prediction is more resilient to adversarial attacks than traditional object-centric systems. In detector-based approaches, a false-negative attack requires only suppressing the confidence score of a single bounding box below the detection threshold. In contrast, for dense voxel grid predictions, an adversary must misclassify all voxels corresponding to a target object into wrong categories, such as _non-occupied_ or _drivable surface_ . This need to simultaneously alter multiple contiguous voxels substantially increases the attack complexity. 

The occupancy predictor Φ takes synchronized multi-view images as input and outputs dense 3D occupancy grids. The predicted grids are represented by a logit tensor _V ∈_ R[17] _[×][I][×][J][×][K]_ , where ( _I, J, K_ ) denote the grid dimensions. For each voxel ( _i, j, k_ ), the tensor encodes logits across 17 semantic classes. Among these, _drivable surface_ and _non-occupied_ are two of the class labels, both indicating the grid where the vehicle is permitted to drive. 

## _B. Programmable Taillights_ 

Programmable lighting systems have been deployed in mass-produced vehicles with over-the-air (OTA) update capabilities, highlighting their technical maturity and commercial viability. For instance, IM Motors has introduced an Interactive Signal Communication (ISC) taillight system with thousands of individually addressable LEDs and multichannel drivers, supporting user-editable patterns and multilevel brightness control [27]. Similarly, Huawei’s AITO M-series vehicles feature the XPIXEL matrix-based taillight platform, which enables high-resolution control and downloadable light signatures [28]. Collectively, these vehicles have exceeded one million deliveries [26]. Examples are illustrated in Fig. 2. The proliferation of such systems emphasizes the real-world significance of programmable taillights as a potential attack vector. 

From a security perspective, programmable taillights offer several operational advantages for visual adversarial attacks. 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

3 

**==> picture [253 x 84] intentionally omitted <==**

images are evaluated by the occupancy network. Gradients from the network’s outputs are computed and used to refine the GAN progressively, yielding the adversarial taillight as the final output. Additionally, to enhance the perceptual naturalness and robustness of the generated taillights, we incorporate color constraints, geometric transformations, and photometric augmentations during training. 

Fig. 2: The programmable taillights in widely saled vehicles. 

## _A. Adversarial Data Preprocessing_ 

First, their pixel-level controllability enables them to function as low-resolution displays, facilitating the generation of adversarial patterns that mimic legitimate taillight designs (e.g., full-width light bars) and thereby achieve naturalistic camouflage against perception systems. Second, as legal vehicle components, they allow an adversarial vehicle to legally position itself ahead of a victim vehicle. Furthermore, adversarial patterns can be activated on demand, providing precise control over the attack’s timing and duration while enhancing stealth, as the taillights appear normal at other times. 

## _C. Attack Model_ 

In this paper, our goal is to cause an autonomous driving system employing an occupancy network to fail in detecting a preceding vehicle equipped with adversarial taillights, resulting in a collision. The attack must also minimize the reaction time available to the human driver, preventing timely intervention to take control and halt the vehicle. Accordingly, we evaluate scenarios where the preceding and victim vehicles are within 3 meters of each other, a distance that affords insufficient reaction time for the human driver. 

We assume that the attacker can manipulate the vehicle’s programmable taillights through one of the following methods: (1) compromising the network-connected vehicular system by exploiting weak passwords or vulnerabilities; (2) intruding into or possessing a vehicle and installing remote-control malware in its systems; or (3) conducting a man-in-the-middle attack to tamper with over-the-air (OTA) update channels and deploy adversarial taillight patterns. In this work, we focus on generating adversarial taillights. 

## III. DESIGN 

In this section, we describe the proposed method for generating adversarial taillights. An overview is illustrated in Fig. 3. To generate adversarial taillights, we assume an adversarial vehicle is present directly ahead in the current frame, and apply perturbations to the contiguous pixel regions of its taillights. Perturbations are then applied to the contiguous pixel regions of the taillight. This process is formulated as an optimization problem to deceive the occupancy network, causing it to overlook the vehicle equipped with the adversarial taillight and thereby induce a collision. 

For optimization, we fine-tune a pretrained GAN to navigate its latent space toward the adversarial sample space of the occupancy network, guided by gradient-based feedback. The GAN iteratively generates perturbed taillight images, which are composited to the adversarial vehicle images. The composited 

**Adversarial Taillight Preprocessing.** We generate the adversarial taillight using a GAN pre-trained with StyleGAN-XL [29]. The generator _Gθ_ maps a low-dimensional latent vector _z_ from its latent space to a high-dimensional image _Gθ_ ( _z_ ). To form the initial taillight, we sample _N_ latent vectors _{z_ 1 _, z_ 2 _, . . . , zN }_ from a standard normal distribution _z ∼N_ (0 _, I_ ) and input them into _Gθ_ . Each latent vector yields a low-resolution RGB taillight segment, and these segments are concatenated horizontally to initialize the taillight: 

**==> picture [238 x 13] intentionally omitted <==**

where Init_Tail denotes the synthesized full-width initial taillight composed of _N_ adjacent segments. 

To generate the adversarial taillights, we fine-tune the GAN to steer its latent space towards the adversarial sample space of the occupancy network following the gradient feedback, which is detailed in Section III-D. 

**Adversarial Taillight Render.** The autonomous driving system of the victim vehicle is equipped with multiple cameras with different views, such as a front-view camera and a left-side camera. Due to perspective effects, the shape of the taillight varies across views. To account for this, we projectively transform the taillight across different views during optimization. Perspective transform goes from 2D to 3D and back to 2D. This is because the 2D taillight image Init_Tail lacks depth information. By mapping points into 3D vehicle coordinate system and aligning it with the taillight plane of the adversarial vehicle, its real-world coordinates can be restored. This allows accurate geometric operations such as rotation and translation. Finally, projecting the points back to 2D of each view produces a view-consistent image that preserves true perspective and spatial relationships. 

First, we calculate the coordinate of _Init_  Tail_ in the 3D victim-vehicle coordinate system. Let [( _u_ 0 _, v_ 0) _, ...,_ ( _unt, vnt_ )] denote the coordinates of pixels of _Init_  Tail_ , we embed it as _CoIT_ = [(0 _, u_ 0 _, v_ 0) _[⊤] , ...,_ (0 _, unt, vnt_ ) _[⊤]_ ] and apply a rigid transformation to align it with the 3D taillight plane. 

**==> picture [201 x 10] intentionally omitted <==**

where 3 _DCoIT_ denotes the 3D coordinates of the taillight pixels in the victim-vehicle coordinate system, _RT_ tail denotes rotation, _PT_ tail represents positional translation. These parameters can be derived from the 3D project sourced from the manufacuturer. 

Second, for each view _v_ , given the intrinsic and extrinsic parameters of that view obtained from the dataset, we project 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

4 

**==> picture [412 x 175] intentionally omitted <==**

**----- Start of picture text -----**<br>
Driving-perspective  Render Attack<br>Scene Dataset<br>Photometric<br>Adversarial Vehicle<br>Adversarial Taillight Augmentation Occupancy<br>Network Φ<br>Pretrained<br>Color<br>Constraints Generator Gθ<br>Calculate Gradients & Update  𝜽<br>**----- End of picture text -----**<br>


Fig. 3: The Overview of the Adversarial Taillight Generation. 

the 3D adversarial taillight points _P ∈_ 3 _DCoIT_ onto the image plane to obtain the 2D image coordinates ( _p_[(] img _[v]_[)] _[∈]_[R][2][).] 

Let _P_[˜] = [ _P[⊤] ,_ 1] _[⊤]_ = [ _X, Y, Z,_ 1] _[⊤]_ denote the homogeneous coordinate of a 3D point _P_ . This projection from the victim-vehicle coordinate system onto the image plane is compactly written as a single matrix multiplication: 

**==> picture [201 x 16] intentionally omitted <==**

where **Π**[(] _[v]_[)] _∈_ R[3] _[×]_[4] is the projection matrix of view _v_ and _∼_ denotes equality up to a non-zero scale, because the third component _w_ ˜[(] _[v]_[)] represents depth in the view frustum and proportional vectors correspond to the same image point. **Π**[(] _[v]_[)] combines the camera’s internal parameters (focal length, optical center) and its external pose (position and orientation relative to the victim vehicle): the extrinsic component rigidly transforms the 3D point from the victim-vehicle coordinate system to the camera coordinate system of view _v_ , and the intrinsic component then maps it onto the image plane. To recover the actual pixel coordinates, we divide by the last component: 

**==> picture [190 x 25] intentionally omitted <==**

Based on the projected 2D coordinates, we then render PT_Init_Tail[(] _[v]_[)] by resampling Init_Tail onto the discrete image grid of view _v_ ; we use bilinear interpolation to obtain smooth, differentiable resampling when the projected sampling locations are non-integer. 

Finally, we remove non-visible pixels using the view-specific taillight visibility mask. Let _M_ screen[(] _[v]_[)][denote][the][visible-region] mask of the taillight in view _v_ , which accounts for view-dependent visibility (e.g., self-occlusion and truncation). We keep only pixels inside the visible region and mask out the remainder, and update the view image by replacing the taillight region with the rendered taillight: 

**==> picture [248 x 13] intentionally omitted <==**

where _I_[(] _[v]_[)] denotes the original image of view _v_ in the current multi-view frame. This step is differentiable because it consists of element-wise multiplication and addition. 

Finally, the adversarial training sample _x_ adv is constructed by aggregating _I_[˜][(] _[v]_[)] across all taillight-visible views. We refer to the overall procedure as a differentiable multi-view taillight rendering pipeline. 

## _B. Loss Function_ 

Our attack objective is to cause the victim vehicle to fail in detecting a preceding vehicle equipped with the adversarial taillights. The victim vehicle uses the occupancy network to predicted the labels of grids within the region of interest (RoI). To acheive the attack goal, all grids within the RoI must be misclassified as _non-occupied_ or _drivable surface_ . 

To derive an adversarial loss function, we thoroughly analyze the architecture of the occupancy network [30]. Typically, it comprises three modules: (i) the 2D feature extraction module extract multi-scale 2D image features from each camera view; (ii) the 3D volume encoding module lifts the 2D image features into 3D space through spatial cross-attention, which serves as the _sole bridge_ from 2D to 3D; and (iii) the 3D volume decoding module upsamples the 3D features and output a dense voxel grid occupancy prediction. 

**Feature Diversity Minimization.** Our experimental analysis (See Section IV-F) reveals that the features among intermidiate layers of the 3D volume encoding module exhibit a consistent asymmetry: the features of _non-occupied_ and _drivable surface_ grids show _low diversity_ , whereas the features of _non-drivable surface_ grids show _high diversity_ (rich structural variation). The reason might be that _non-occupied_ and _drivable surface_ grids indicate empty while the _non-drivable surface_ grids indicate some obstacles exist. The _non-drivable surface_ grids has more details compared with _non-occupied_ and _drivable surface_ grids, and thus their features presents more diverse. Hence, by Minimizing the feature diversity during adversarial generation, the adversarial taillight could erase the features of _non-drivable surface_ grids and induces the occupancy network to misclassify the region as drivable. 

Formally, let _Z_[(] _[u]_[)] denote the feature tensor of _u_ -th layer of the 3D volume encoding module, where _Z_[(] _[u]_[)] _∈_ R _[C]_[(] _[u]_[)] _[×][D]_[(] _[u]_[)] _[×][H]_[(] _[u]_[)] _[×][W]_[ (] _[u]_[)] , _u ∈{_ 1 _,_ 2 _, . . . , U }_ , and _U_ is the 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

5 

total number of middle layers of the 3D volume encoding module. We extract the feature vectors within the RoI of _Z_[(] _[u]_[)] and arrange them as columns to form a matrix **Z**[(] roi _[u]_[)][=] [ **z** 1 _,_ **z** 2 _, . . . ,_ **z** _N_ ( _u_ )], where **z** _i ∈_ R _[C]_[(] _[u]_[)] is the feature vector of the _i_ -th grid and _NR_[(] _[u]_[)] denotes the total number of grids within the RoI of layer _u_ . The feature diversity of the _u_ -th intermediate layer’s output is defined as [31]: 

**==> picture [176 x 14] intentionally omitted <==**

where _∥·∥F_ denotes the Frobenius norm, ¯ **z** = _NR_ 1[(] _[u]_[)] � _NRi_ =1[(] _[u]_[)] **z** _i_ is the spatial mean, and **1** is the all-ones vector of dimension _NR_[(] _[u]_[)] . A smaller _σ_[(] _[u]_[)] indicates that the RoI features are more homogeneous; when _σ_[(] _[u]_[)] = 0, all feature vectors converge to the same mean vector and the information diversity is completely eliminated. 

The loss of feature diversity minimization is defined as: 

**==> picture [184 x 30] intentionally omitted <==**

Minimizing _L_ diver actively compresses the residual feature energy in the RoI, inducing the intermediate features of the adversarial vehicle to mimic the homogeneous characteristics of an empty drivable surface. 

**Occupancy Constraint.** At the final output stage, we enforce a hard constraint that every grid within the RoI must be classified as _non-occupied_ or _drivable surface_ . We formalize this requirement as an adversarial objective in voxel space. 

The victim model Φ outputs classification results for dense 3D voxel grids, represented by a logit tensor _V_ . Each grid at index ( _i, j, k_ ) in _V_ contains a 17-dimensional logit vector: 

**==> picture [210 x 15] intentionally omitted <==**

where _z_ 1[(] _[i,j,k]_[)] denotes the _non-occupied_ class and _z_ 12[(] _[i,j,k]_[)] denotes the _drivable surface_ class. 

The grid is permitted to be driven on if it is classified as _non-occupied_ or _drivable surface_ . The occupancy network-based autonomous driving system navigates the vehicle toward a RoI if all grids within the RoI are classified as _non-occupied_ or _drivable surface_ . To achieve the attack objective, we target _V_ such that the maximum logit for each grid in the RoI corresponds to the _non-occupied_ or _drivable surface_ class. This induces the occupancy network to misclassify all RoI grids as _non-occupied_ or _drivable surface_ , prompting the victim vehicle to collide with the adversarial vehicle. 

Formally, the local success condition for a grid ( _i, j, k_ ) is: 

**==> picture [243 x 32] intentionally omitted <==**

where _C_ ( _i,j,k_ ) is an indicator that expresses whether the equation holds, _z_ 1[(] _[i,j,k]_[)] represents the _non-occupied_ class, _z_ 12[(] _[i,j,k]_[)] denotes _drivable surface_ class, respectively. The condition checks whether the maximum element in the sequence � _z_ 1[(] _[i,j,k]_[)] _, z_ 2[(] _[i,j,k]_[)] _, . . . , z_ 17[(] _[i,j,k]_[)] � is either _z_ 1[(] _[i,j,k]_[)] or _z_ 12[(] _[i,j,k]_[)] . If this condition holds, the attack successfully fools the model into misclassifying the grid as permitted to be driven on. 

The adversarial loss for grid ( _i, j, k_ ) is defined as: 

**==> picture [251 x 55] intentionally omitted <==**

where _z_ vehicle[(] _[i,j,k]_[)][is][the][logit][for][the] _[vehicle]_[class,] _[z]_ non[(] _[i,j,k]_[)] denotes the logit of _non-occupied_ class, _z_ drv[(] _[i,j,k]_[)] denotes the logit of _drivable surface_ class,0 _≤ w_ 2 _≤ w_ 1 _≤_ 1 are weighting coefficients, and Softplus( _x_ ) = log(1 + _e[x]_ ). This formulation yields zero loss when _C_ ( _i,j,k_ ) holds and otherwise imposes a smooth penalty that increases with deviation, suppressing the _vehicle_ logit in favor of the _non-occupied_ and _drivable surface_ logits. 

The victim model’s predictions are confined to the RoI, which comprises multiple voxel grids. To induce misclassification across the entire RoI, we aggregate the per-grid losses into the adversarial loss: 

**==> picture [179 x 25] intentionally omitted <==**

Minimizing _L_ adv increases the likelihood that all RoI grids satisfy Eq. (9), thereby achieving the attack objective of misclassifying the obstacle region as drivable. 

**Overall Loss.** Combining the feature diversity minimization loss and occupancy constraint loss, the overall optimization objective is defined as: 

**==> picture [166 x 10] intentionally omitted <==**

## _C. Naturalness and Robustness_ 

**Color Constraints.** To enhance the naturalness of the generated adversarial samples, we impose color constraints during the generation process. These constraints enforce two conditions: (i) the blue channel is set to zero for all pixels in the RGB color space, as LED taillights appear unnatural when emitting blue light; and (ii) the hue is restricted to the interval [0 _, H_ max] in the Hue-Saturation-Value (HSV) color space, ensuring red-dominant and visually plausible tones. 

The color transformation proceeds as follows: the original RGB representation is first processed by masking the blue channel (i.e., setting it to zero using an offline mask to avoid in-place modifications). The resulting RGB tensor is then converted to HSV space via a differentiable RGB-to-HSV mapping. In HSV space, the hue component is smoothly clamped to the range [0 _, H_ max], preserving continuous transitions and perceptual coherence. Finally, the modified HSV representation is transformed back to RGB space, yielding the constrained output. 

This pipeline is fully differentiable, as it avoids in-place operations, conditional branching, and discrete decisions, relying instead on smooth element-wise arithmetic, continuous approximations, and offline masking. As a result, gradients propagate stably throughout, enabling compatibility with end-to-end gradient-based optimization. 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

6 

**Geometric Transformation.** To enhance the robustness of the generated adversarial samples, we vary multiple geometric parameters during the generation process. 

These parameters are represented by ( _h, d, ℓ_ ), where _h_ is the mounting height of the front-view camera on the victim vehicle, _d_ is the longitudinal distance from the camera to the taillight of the adversarial vehicle, and _ℓ_ is the lateral offset between the centers of the two vehicles. They describe the spatial configuration between the victim vehicle and the adversarial vehicle. 

The values of ( _h, d, ℓ_ ) are randomly sampled from uniform distributions and are applied in the generation. This approach ensures that our dataset contains the samples with different geometric configurations, thereby promoting consistent robustness against arbitrary spatial variations. 

**Photometric Augmentation.** To enhance the robustness of the generated adversarial samples, we vary photometric parameters during the generation process. Specifically, we adjust illumination and exposure by applying a differentiable affine brightness transformation. 

For an adversarial image _x_ adv, we uniformly sample augmentation parameters ( _α, β_ ) at each training iteration, where _α_ controls global brightness scaling and _β_ adjusts exposure bias. The augmented input is: 

**==> picture [171 x 12] intentionally omitted <==**

where **1** is an all-ones tensor matching the dimensions of _x_ adv. 

By sampling ( _α, β_ ) uniformly from their continuous ranges across iterations, the model encounters a broad spectrum of lighting conditions, from underexposed to overexposed. This promotes robustness to real-world variations in illumination and sensor responses. 

The linear nature of the affine transformation preserves differentiability with respect to pixel values and upstream parameters, enabling stable gradient propagation through the photometric pipeline. 

## _D. Adversarial Taillights Generation_ 

The generator _Gθ_ is trained to produce adversarial taillights that minimize _L_ , thereby inducing the victim model Φ to classify the attack objective in RoI. The optimization objective is: 

**==> picture [187 x 20] intentionally omitted <==**

where _x[′]_ adv[denotes][the][multi-view][adversarial][input][and] _[L]_[is] the overall loss defined in Eq. (12). Figure 3 illustrates the adversarial fine-tuning pipeline. 

Algorithm 1 details the generation process. In each training iteration, a batch _{dpdata}[m] i_ =1[is][sampled][from] _[DP]_[Data] (Line 4). For each batch item, a latent vector _z_ is sampled from _N_ (0 _, I_ ) and passed through _Gθ_ to yield the initial taillight Init_Tail (Lines 6–7). Color constraints are then applied using _H_ max to produce Init_Tail _[′]_ (Line 8). This taillight is rendered onto the front-view image of _dpdata_ via the composition function _T_  composition_ , and combined with other views to form the adversarial input **x** adv (Line 9). Photometric parameters ( _α, β_ ) are uniformly sampled via _S_  Uniform_ 

**Algorithm 1** The algorithm of Adversarial Taillights Generation **Input:** Pretrained generator _Gθ_ ; Victim model Φ; Driving-perspective Data _DP_ Data; Naturalness Parameters _H_ max. 

**Output:** Adversarial Taillight _Adv_  Tail_ 

- 1: Initialize _basr ←_ 0; _nbatch ←_ 0; _θ[⋆] ← θ_ ; 

- 2: Adversarial Taillight _Adv_  Tail ←∅_ 

3: **for** _epoch ∈ Epoch_ **do** 4: Sample a Batch _{dpdata}[m] i_ =1[from] _[DP]_[Data] 5: **for** _batch ∈ Batch_ **do** 6: _z ∼N_ (0 _, I_ ) 7: _Init_  Tail ← Gθ_ ( _z_ ). 8: _Init_  Tail[′] ← ColorConstraint_ ( _Init_  Tail_ , _H_ max) 9: _xadv_ = T_render( _dpdata_ , _InitTail[′]_ ) 10: ( _α, β_ ) _← UniformSampling_ () 11: **x** _[′]_ adv[=][Photo_Aug(] _[x][adv]_[,][(] _[α, β]_[)][)] 12: _L ← Compute_  Loss_ (Φ( **x** _[′]_ adv[))][.] 13: **end for** 14: Update _θ_ with Adam: _θ ←_ Adam( _∇θL, θ_ ). 15: _nbatch_ += 1. 16: **if** _nbatch_ mod 10[4] = 0 **then** 17: _ASR ←_ Attack_Test( _θ_ ). 18: ( _Best_  ASR, θ[⋆]_ ) _←_ Update( _ASR, θ_ ). 19: **end if** 20: **end for** 

21: _Gθ⋆ ← Update_ ( _G, θ[⋆]_ ) 

22: _Adv_  Tail ← ColorConstraint_ ( _Gθ⋆_ ( _z_ ); _Hmax_ ) 

23: **return** _Adv_  Tail_ . 

from the ranges (0.7, 1.3) and (-30, 30), respectively (Line 10), and applied to **x** adv to obtain the augmented input **x** _[′]_ adv (Line 11). This input is forwarded through the victim model Φ to compute the overall loss _L_ (Line 12), with gradients _∇θL_ backpropagated and accumulated across the batch. After processing all _m_ items, the averaged gradients update _θ_ using the Adam optimizer (Line 14). 

During training, the attack success rate (ASR)—the proportion of samples where all RoI grids are misclassified as _non-occupied_ or _drivable surface_ —is periodically evaluated, and the parameters _θ[⋆]_ yielding the highest ASR are retained (Lines 15–19). Post-training, _G_ is updated with _θ[⋆]_ , the final adversarial taillight Adv_Tail is generated under color constraints, and it is output for adversarial attacks. (Lines 21–23). 

## IV. EVALUATION 

Experiments were conducted on Ubuntu 22.04.5 LTS using dual NVIDIA RTX 4090 GPUs. The maximum number of training epochs was set to 10, with early stopping if the ASR did not improve over two consecutive validation epochs. Each training batch consisted of 16 frames sampled from the nuScenes dataset. For each frame, _Gθ_ generated 11 adversarial segments, which are concatenated to an initial taillight. The default HSV parameters is set to _H_ max = 20. Optimization employed the Adam optimizer with _β_ 1 = 0 _._ 9, _β_ 2 = 0 _._ 999, and a learning rate of 0.0002. 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

7 

We define the RoI as a physical space ahead of the victim vehicle with dimensions 2 m (width), 5 m (length), and 2.0 m (height), exceeding typical vehicle sizes. For loss computation, the RoI spanned grid indices 98–101 (lateral), 104–113 (longitudinal), and 6–9 (vertical). We set _w_ 1 _> w_ 2 because most RoI grids correspond to _non-occupied_ regions (space above ground), while only a subset near the ground corresponds to _drivable surface_ , aligning the margins with expected occupancy distributions in realistic driving scenes. The weights _w_ 1 and _w_ 2 were varied from 0 to 1, yielding the optimal pair: _w_ 1 = 1 _._ 0 and _w_ 2 = 0 _._ 8. 

**Dataset.** The experiments are conducted on the nuScenes dataset [32], which comprises 850 scenes (700 for training and 150 for validation) spanning 34,149 keyframes. For training, we used the full official training split. For evaluation, we selected a balanced subset of 1,050 frames (seven per validation scene). To construct the driving-perspective dataset, we composited vehicles from our vehicle dataset into the front-view camera images from nuScenes. 

**Driving-perspective Data Generation.** The 3D vehicle model, featuring programmable taillights, is sourced from the manufacturer. Using 3ds Max with MaxScript automation, we load the model into the victim-vehicle coordinate system and render multi-view frames in which an adversarial vehicle is placed directly ahead. 

To ensure uniform coverage of geometric configurations, we discretize each parameter into a finite set of values and sample tuples ( _h, d, ℓ_ ) uniformly from the resulting discrete grid, generating one frame per sampled tuple. 

Along with the rendered multi-view images, we export the associated data required by the differentiable multi-view taillight rendering pipeline: (i) the rigid transformation ( _R_ tail _, t_ tail) from the initial reference plane to the adversarial vehicle taillight plane, obtained via real-world measurement and combined with the current ( _h, d, ℓ_ )-dependent pose; (ii) the per-view calibration parameters _K_[(] _[v]_[)] and ( _R_[(] _[v]_[)] _, t_[(] _[v]_[)] ); and (iii) the view-specific taillight visibility mask _M_ screen[(] _[v]_[)][,][obtained] by annotating the visible taillight region in each view. 

**Evaluation Metrics.** The attack success rate (ASR) measures the proportion of images in which the adversarial vehicle with taillights appears invisible to the victim vehicle’s occupancy network [30]. Specifically, an attack succeeds if all grids within the RoI are misclassified as _non-occupied_ or _drivable surface_ , despite the RoI being physically occupied by the adversarial vehicle. This misclassification induces the victim vehicle to navigate into the region, potentially causing a collision. 

## _A. Attack Effectiveness_ 

Our adversarial objective is to render the vehicle equipped with adversarial taillights invisible in the victim model’s output—that is, to induce the model to classify all grids occupied by the adversarial vehicle as _non-occupied_ or _drivable surface_ . To assess the method’s effectiveness, we compared the victim model’s outputs for two input types: (1) vehicles without adversarial taillights and (2) vehicles with adversarial taillights. Overall, the model accurately detects unattacked vehicles but 

fails to detect those with adversarial taillights, achieving an average ASR of 93.11%. 

**==> picture [253 x 142] intentionally omitted <==**

Fig. 4: The prediction results of occupancy network. Top: vehicles without adversarial taillights; Bottom: vehicles with adversarial taillights. The yellow dashed rectangle approximately indicates RoI. The blue color represents _vehicle_ class. 

**==> picture [81 x 46] intentionally omitted <==**

**==> picture [81 x 46] intentionally omitted <==**

**==> picture [81 x 46] intentionally omitted <==**

**==> picture [81 x 46] intentionally omitted <==**

**==> picture [81 x 46] intentionally omitted <==**

**==> picture [81 x 46] intentionally omitted <==**

Fig. 5: The prediction results of occupancy network in 3D bounding boxes. Top: vehicles without adversarial taillights; Bottom: vehicles with adversarial taillights. 

Figure 4 presents the occupancy prediction results as 3D color images, with predicted labels visualized. The yellow dashed rectangle denotes the RoI, and blue represents the _vehicle_ class. The model correctly identifies unattacked vehicles (evident in blue regions) but assigns erroneous labels under attack, causing the blue regions to disappear within the RoI. For intuitive visualization, we converted the occupancy predictions into 3D bounding boxes using connected component analysis [33]. The 3D bounding boxes enclose the detected objects. These bounding box results, shown in Figure 5, confirm that the occupancy network accurately detects vehicles in unattacked images. In contrast, the model erroneously processes images of vehicles with adversarial taillights, failing to detect the adversarial vehicles while correctly identifying other vehicles and objects. 

Moreover, we present heatmaps of the predicted results without attack (Fig. 6) and with attack (Fig. 7), where high-intensity areas (red) indicate vehicle predictions. The heatmap in Fig. 6 shows predicted areas concentrated in a compact region aligned with the RoI. In contrast, the heatmap in Fig. 7 reveals deviations in these areas under adversarial attack. 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

8 

TABLE I: The impact of parameters (distance, camera height, lateral offset) to the ASR (%). 

||Distance (m)<br>Camera Height (m)<br>Lateral Offset (m)<br>ASR (%)|
|---|---|
||1.6<br>1.3<br>-0.3<br>99.05<br>1.6<br>1.3<br>0.0<br>99.90<br>1.6<br>1.3<br>+0.3<br>99.81<br>1.6<br>1.5<br>-0.3<br>95.52<br>1.6<br>1.5<br>0.0<br>90.00<br>1.6<br>1.5<br>+0.3<br>94.48<br>2.4<br>1.5<br>-0.3<br>99.52<br>2.4<br>1.5<br>0.0<br>99.90<br>2.4<br>1.5<br>+0.3<br>99.71<br>3.2<br>1.5<br>-0.3<br>69.24<br>3.2<br>1.5<br>0.0<br>83.14<br>3.2<br>1.5<br>+0.3<br>87.05|
||Average ASR<br>93.11|



**==> picture [253 x 255] intentionally omitted <==**

**----- Start of picture text -----**<br>
115 XY Plane Projection XZ Plane Projection<br>10 [3] 10 . 0 10 [3]<br>110 10 [2] 7 . 5 10 [2]<br>10 [1] 5 . 0 10 [1]<br>105<br>10 [0] 10 [0]<br>95 100 105 95 100 105<br>X axis X axis<br>Fig. 6: The Heatmap of occupancy prediction frequency without<br>attack. Left: Top-down (XY) projection; Right: Rear (XZ)<br>projection. The red dashed rectangle marks the RoI.<br>115 XY Plane Projection XZ Plane Projection<br>10 [3] 10 . 0 10 [3]<br>110 10 [2] 7 . 5 10 [2]<br>10 [1] 5 . 0 10 [1]<br>105<br>10 [0] 10 [0]<br>95 100 105 95 100 105<br>X axis X axis<br>Y axis Z axis<br>Occupancy Count Occupancy Count<br>Y axis Z axis<br>Occupancy Count Occupancy Count<br>**----- End of picture text -----**<br>


Fig. 7: The Heatmap of occupancy prediction frequency with attack. Left: Top-down (XY) projection; Right: Rear (XZ) projection. The red dashed rectangle marks the RoI. 

## _B. Attack Robustness_ 

We evaluated the robustness of our attack under diverse geometric and photometric configurations. Geometric parameters were sampled from discrete sets: camera height _h ∈{_ 1 _._ 3 _,_ 1 _._ 5 _}_ m, inter-vehicle distance _d ∈{_ 1 _._ 6 _,_ 2 _._ 4 _,_ 3 _._ 2 _}_ m, and lateral offset _ℓ ∈{−_ 0 _._ 3 _,_ 0 _._ 0 _,_ +0 _._ 3 _}_ m. Photometric parameters _α_ and _β_ were uniformly sampled from (0.7, 1.3) and (-30, 30), respectively. 

These parameters reflect real-world variability. Camera height accounts for differences in sensor mounting across vehicle models; inter-vehicle distance captures positioning uncertainty in dynamic traffic, where precise spacing is often infeasible; and lateral offset models deviations from perfect lane-center alignment. Collectively, they characterize the 3D 

TABLE II: The impact of parameters (photometric augmentations) to the ASR (%). 

||**Weather**|_α_|_β_|**ASR (%)**|
|---|---|---|---|---|
|||1.3|30|91|
||Sunny|1.3|0|92|
|||1.3|-30|93|
|||1.0|30|93|
||Cloudy|1.0|0|94|
|||1.0|-30|96|
|||0.7|30|95|
||Overcast|0.7|0|96|
|||0.7|-30|99|



TABLE III: The ASR (%) of our method against different defense methods. “No defense” is reported as baseline. 

||**Defense Type**|**Best ASR (%)**|
|---|---|---|
||Bit Depth Reduction [34]<br>Gaussian Smoothing [35]|89.9<br>86.9|
||Total Variance Minimization [36]<br>Median Blur [37]<br>TVM+SS [38]<br>APD-TFD [39]<br>Mem-AE [40]<br>**No Defense**|91.6<br>88.2<br>86.7<br>92.5<br>87.3<br>93.1|



spatial relationship between the adversarial vehicle and victim vehicle, encompassing vertical (mounting height) and horizontal (distance and offset) dimensions. 

As listed in Table I, performance remains near-perfect ( _ASR ≥_ 92%) at closer distances (1.6m and 2.4m). However, a significant decline is observed at the furthest distance of 3.2m, where the ASR drops to between 71% and 87%. At distances of 1.6m, reducing the camera height from 1.5m to 1.3m improved or maintained a 100% ASR across all offsets. This suggests a lower height might be beneficial for short-range operations. No consistent performance trend is observed across different offsets. For instance, at 3.2m with a height of 1.5m, performance is weakest at a -0.3m offset (71% ASR) and improves at the 0.0m and +0.3m offsets. The highest ASR occurred not only at the closest distance (1.6 m) but also at _d_ = 2 _._ 4 m, and not only at zero offset but only at slight offsets ( _ℓ_ = _±_ 0 _._ 3 m). This indicates that attack effectiveness does not increase monotonically with proximity or alignment, possibly due to taillight occlusion or reduced receptive field coverage in the voxel grid at extreme positions. 

For photometric robustness, we simulated varied lighting using the augmentation in Eq. (13). For example, ( _α_ = 1 _._ 3 _, β_ = 30) approximates bright overcast daylight, while ( _α_ = 0 _._ 7 _, β_ = _−_ 30) mimics dim conditions like dusk or shaded roads. As shown in the left panel of Table II, the attack maintained high effectiveness, with ASR consistently above 91%. Performance improved under darker conditions, likely because the adversarial taillight becomes more salient against low-luminance backgrounds. 

## _C. Impact of Active Pixel Ratio on ASR_ 

We investigated the impact of the proportion of active adversarial pixels on attack efficacy. For each inter-vehicle 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

9 

**==> picture [155 x 109] intentionally omitted <==**

**----- Start of picture text -----**<br>
1 . 0<br>d = 1.6<br>0 . 8 d = 2.4<br>d = 3.2<br>0 . 6<br>0 . 4<br>0 . 2<br>0 . 0<br>0 20 40 60 80 100<br>Effective Pixel Percentage (%)<br>ASR (%)<br>**----- End of picture text -----**<br>


Fig. 8: The ASR of our method with different the active-pixel percentage. d represents the vehicle distances. 

distance (1.6 m, 2.4 m, and 3.2 m), we sorted pixels in the adversarial taillight region by brightness in the HSV value channel. The top _p_ % ( _p ∈{_ 10 _,_ 20 _, . . . ,_ 100 _}_ ) pixels were retained unchanged, while the brightness values of the remaining pixels were set to zero. This setup simulates a taillight with only a fraction of LEDs illuminated, enabling assessment of the effective adversarial pixel ratio’s influence. 

As shown in Fig. 8, the relationship between the effective pixel percentage _p_ and ASR exhibits three regimes. For _p ≥_ 80%, ASR remains consistently high with negligible variation, as removed low-brightness pixels contribute minimally to the adversarial taillight. Between _p_ = 70% and _p_ = 40%, ASR decreases progressively with reduced pixel count, indicating gradual degradation in effectiveness. Below _p_ = 40%, ASR drops to near zero, suggesting that insufficient pixel density undermines adversarial efficacy. 

Across all ratios, the relative performance by distance mirrors the trends in Section IV-B: _d_ = 2 _._ 4 m consistently yields the highest ASR, followed by 1.6 m and 3.2 m, confirming robustness to perturbation sparsity in favorable geometric arrangements. 

## _D. Resilience Against SOTA Defense Methods_ 

We evaluated the resilience of our attack against state-of-the-art (SOTA) defense methods. Specifically, we implemented four general defenses:Bit-depth Reduction [34], Total Variance Minimization (TVM) [36], Gaussian smoothing [35], and Median blur method [37]. We also implemented three specialized defenses for autonomous driving: TVM+SS [38], which combines total variance minimization with spatial smoothing; APD-TFD [39], which employs texture feature detection with local denoising; and Mem-AE [40], which uses memory-augmented autoencoders to store normal feature patterns. 

As summarized in the right panel of Table III, our attack exhibited strong resilience, with ASRs consistently exceeding 86% across all defenses. For example, APD-TFD method yielded a high ASR of 92.5%, bit depth reduction has an ASR of 89.9%, total variance minimization method has an ASR of 91.6%, gaussian smoothing method has an ASR of 86.9%, median blur method has an ASR of 88.2%, while TVM+SS method provided the strongest protection, with an ASR of 86.7%. 

TABLE IV: Ablation experiments on loss hyperparameters ( _w_ 1 _, w_ 2). 

|||
|---|---|
|_w_1<br>_w_2|**ASR (%) at Lateral Offset** _l_ **(m)**<br>**-0.30**<br>**0.00**<br>**+0.30**|
|1.0<br>0.8<br>0.8<br>0.7<br>1.0<br>0.4<br>0.5<br>0.3<br>0.3<br>0.1<br>1.0<br>0.0|97<br>94<br>87<br>95<br>94<br>84<br>96<br>92<br>86<br>96<br>93<br>84<br>95<br>94<br>85<br>94<br>92<br>83|



TABLE V: Ablation experiments on different generator architectures and HSV hue constraint _Hmax_ . 

||||**ASR **|**(%) at **|**Offset** _l_ **(m)**|
|---|---|---|---|---|---|
||**Architecture**|_Hmax_|**-0.30**|**0.00**|**+0.30**|
||StyleGAN-XL|5|92|90|88|
||StyleGAN-XL<br>StyleGAN-XL<br>R3-GAN|20<br>40<br>40|97<br>94<br>_<_1|94<br>92<br>_<_1|87<br>87<br>_<_1|



Among the specialized defenses, TVM+SS offered moderate protection (ASR = 86.7%) owing to its combined denoising strategy, which outperformed individual techniques. APD-TFD showed limited effectiveness (ASR = 92.5%), as its texture-based detection struggles with the smooth, natural appearance of our adversarial taillights. Mem-AE performed similarly to other smoothing-based methods (ASR = 87.3%), with its memory-augmented reconstruction providing only marginal gains. These findings indicate that both general and specialized defenses offer limited protection against our physically plausible adversarial taillights. 

TABLE VI: The ASR(%) with different generator depth and lateral offsets. 

|offsets.||
|---|---|
|**Layers**|**ASR (%) at Lateral Offset** _l_ **(m)**<br>**-0.30**<br>**0.00**<br>**+0.30**|
|11<br>9|97<br>94<br>87<br>97<br>88<br>81|



**==> picture [81 x 48] intentionally omitted <==**

**==> picture [81 x 48] intentionally omitted <==**

**==> picture [81 x 48] intentionally omitted <==**

Fig. 9: Visual comparison of adversarial taillights generated by the GAN under varying hue constraints _H_ max. From left to right: _H_ max = 40, 20, and 5. 

## _E. Ablation Study_ 

We conducted ablation experiments to assess the impact of key components in our attack pipeline: (1) the pre-trained generator architecture, (2) the HSV color constraint parameter _H_ max, and (3) the loss function hyperparameters ( _w_ 1, _w_ 2). 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

10 

_1) Generator Architecture:_ Our method uses StyleGAN-XL [29] as the generator. This architecture substantially outperforms R3-GAN, achieving an ASR exceeding 90% across all lateral offsets (see Table V), compared to below 1% for R3-GAN. Moreover, StyleGAN-XL generates adversarial taillights with simpler patterns, consistent styling, smooth color transitions, and uniform chromaticity—attributes essential for physical plausibility and visual naturalness. 

_2) Color Constraint (Hmax):_ The HSV hue constraint, governed by _H_ max, is critical for ensuring visual plausibility and attack efficacy. Moderate reductions in _H_ max typically degrade the ASR due to overly restrictive color ranges. Among tested values, _H_ max = 20 yields the highest ASR, outperforming both higher and lower settings. (See Fig. 9 and Table V) 

_3) Generator Depth:_ We further examined the impact of generator depth by varying the number of synthesis layers from 9 to 11. Increasing the depth consistently improved the ASR, particularly at the central offset ( _ℓ_ = 0 _._ 0 m), where it rose from 88% to 94% (Table VI). This indicates that deeper architectures provide a richer parameter space, enabling better convergence to effective adversarial configurations. 

_4) Loss Function Hyperparameters:_ Our adversarial loss incorporates two weighted terms: _w_ 1 promotes classification of the target grid region as _non-occupied_ , while _w_ 2 encourages classification of road-adjacent grids as _drivable surface_ . As listed in Table IV, variations in these coefficients have minimal impact on the ASR, demonstrating our method’s robustness to weight changes. The combination ( _w_ 1 _, w_ 2) = (1 _._ 0 _,_ 0 _._ 8) yields optimal performance, achieving the highest ASR across all evaluated lateral offsets. 

_5) Loss Function Composition:_ To validate the contribution of each loss component, we conducted ablation experiments on the loss function composition. We evaluated three configurations: (i) only the adversarial objective loss ( _L_ adv), (ii) only the diversity collapse loss ( _L_ diver), and (iii) the full combination ( _L_ diver + _L_ adv). All ablation variants were trained with identical hyperparameters and the same random seed. We report the best validation ASR across epochs to ensure fair comparison. 

As summarized in Table VII, both individual loss components achieve high ASRs in isolation. The diversity collapse loss alone reaches 87.33%, 74.86%, and 93.14%, while the adversarial objective loss alone attains 94.00%, 86.86%, and 90.38%. _L_ diver surpasses _L_ adv at the +0 _._ 30 m offset, whereas _L_ adv outperforms _L_ diver at the _−_ 0 _._ 30 m and 0 _._ 00 m offsets. By combining these two losses, the model achieves the optimal ASR at every geometric setting. These results demonstrate the superiority of the joint loss design: unifying intermediate encoding regularization with final decoding-stage adversarial guidance yields not only the highest ASR at every offset but also the most robust performance across varying geometric configurations. 

## _F. Feature Diversity Analysis_ 

The full 3D volume spans 200 _×_ 200 _×_ 16 grids, and the RoI is defined as grid indices _x ∈_ [98 _,_ 101], _y ∈_ [104 _,_ 113], 

TABLE VII: Ablation experiments on loss function composition. 

|VII:<br>Ablation<br>n.|experiments<br>on<br>loss|
|---|---|
|**Loss Function**|**ASR (%) at Offset** _l_ **(m)**<br>**-0.30**<br>**0.00**<br>**+0.30**|
|_L_adv<br>_L_diver<br>_L_diver+_L_adv|94.00<br>86.86<br>90.38<br>87.33<br>74.86<br>93.14<br>95.52<br>90.00<br>94.48|



_z ∈_ [6 _,_ 9], occupying 10 _×_ 4 _×_ 4 = 160 grids in the output space. The intermidiate layers of the 3D volume encoding module down-sampling the 3D grids from full 3D volume. The expected number of feature grids covering the RoI in each layer follows directly from proportional scaling. Hence, we select the layers whose output volume is larger than 30 _×_ 30 _×_ 4 to ensure the number of grids covering the RoI is is larger than one. 

Furthermore, before layer normalization, the Frobenius norm _σ_[(] _[u]_[)] is dominated by feature energy which can be inflated by unrelated factors, masking structural differences. After layer normalization, energy interference is suppressed and _σ_[(] _[u]_[)] begins to measure structural diversity. _non-drivable surface_ features encode richer geometric and textural variations and resist being minimized into a homogeneous representation, yielding heavier-tailed post-norm distributions with larger _σ_[(] _[u]_[)] . In contrast, _non-occupied_ and _drivable surface_ features are more readily minimized into concentrated distributions with smaller _σ_[(] _[u]_[)] . Hence, the layers before normalization layers are unselected. Finally, the selected layers include: normalization layers, the feed-forward network, the 3D convolution layers, and the residual connection. 

To explain the reasonale of feature diversity minimization to adversarial generation, we randomly select 1K samples from our dataset and extract their features vectors of intermidiate layers of the 3D volume encoding module. The feature diversity _σ_ (Eq. (6)) of these features vectors could be classified into three types: _empty_ ( _non-occupied_ or _drivable surface_ ), _occupy_ ( _non-drivable surface_ , with preceding vehicle), and _attack_ (with adversarial preceding vehicle). We evaluate two types of differences (∆Benign,∆Attack) to illustrate the reasonale of feature diversity minimization to adversarial attack: 

(i) ∆Benign = _σ_ empty _− σ_ occupy. Each difference is calculated on a sample pair with the same background, where the _empty_ sample do not has the preceding vehicle while the _occupy_ sample has a preceding vehicle. For a sample pair, _σ_ empty _< σ_ occupy (∆Benign _<_ 0), indicates _non-occupied_ and _drivable surface_ grids has lower feature diversity while _non-drivable surface_ grids has higher feature diversity. Fig. 10 visualizes the ∆ distributions for four selected layers’ outputs. _σ_ empty _< σ_ occupy (∆Benign _<_ 0) holds for over 95% of sample pairs. This overwhelming prevalence confirms that the feature diversity in emphnon-occupied and _drivable surface_ is consistently lower than in _non-drivable surface_ . 

(ii) ∆Attack = _σ_ attack _− σ_ occupy. Each difference is calculated on a sample pair with the same background and preceding vehicle, where the _occupy_ sample do not has the adversarial taillight while the _attack_ sample has the adversarial taillight. 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

11 

TABLE VIII: Diversity collapse analysis on the 14 selected intermediate layer outputs. ∆ _<_ 0 (%): proportion of samples with diversity reduction under benign and attack conditions, respectively. Analysis based on 896 matched samples per output. 

|**Block**|**Layer**|∆Benign (%)|∆Attack (%)|
|---|---|---|---|
|0|norm1|97.4|83.0|
|0|ffn|96.4|91.1|
|0<br>0<br>0<br>1<br>1<br>1<br>2|norm2<br>conv<br>layer-out<br>norm2<br>conv<br>layer-out<br>norm2|97.3<br>96.9<br>97.8<br>100.0<br>90.8<br>95.6<br>99.4|89.3<br>97.3<br>93.8<br>85.6<br>100.0<br>99.5<br>82.7|
|2|conv|99.9|81.5|
|2|layer-out|99.7|82.6|
|3<br>3<br>3|norm2<br>conv<br>layer-out|100.0<br>98.3<br>99.9|91.5<br>99.4<br>99.2|



For a sample pair, _σ_ attack _< σ_ occupy ( ∆Attack _<_ 0) indcates that the adversarial attack is achieved by decreasing the feature diversity of the RoI grids. Fig. 10 visualizes the ∆ distributions for four selected layers’ outputs. _σ_ attack _< σ_ occupy (∆Benign _<_ 0) holds for over xx% of sample pairs. This overwhelming prevalence indicates that our attack benifits from feature diversity minimization. 

## _G. Interpretability Analysis_ 

To elucidate the mechanism by which adversarial taillights manipulate occupancy predictions, we conducted three analytical approaches: (i) Grad-CAM visualization to visualize the model’s attention; (ii) intermediate-layer diversity statistics to quantify the effect of the diversity collapse loss; and (iii) generator fine-tuning progression to examine the evolution of adversarial taillights during training. 

_1) Grad-CAM Visualization:_ Grad-CAM heatmaps were generated for the entire validation dataset and overlaid on the corresponding input images to visualize influential regions. As shown in Fig. 11, the taillight region dominates the model’s attention, exhibiting consistently high Grad-CAM values that indicate its substantial role in misclassifying adversarial vehicle grids as _non-occupied_ . Notably, high-value distributions within the taillight vary across backgrounds without localizing to specific areas. In some cases, non-vehicle background regions also show elevated values, likely due to the model’s use of global information via cross-attention mechanisms. 

_2) Generator Fine-tuning Progress:_ We examined the generator’s performance during fine-tuning, as measured by the number of training images and ASR. Figure 12 illustrates outputs at varying training image counts. The progression from Fig. 12(a) to Fig. 12(f) shows that increasing the number of training images generally correlates with higher validation ASR, rising from 0.75% to 95%. Once the ASR reached or exceeded 78%, the generator’s output stabilized, with visual patterns remaining largely unchanged thereafter. 

**==> picture [251 x 396] intentionally omitted <==**

**----- Start of picture text -----**<br>
100 r < 0: 96.4% 100 r < 0: 91.1%<br>50 50<br>0 0<br>20 0 20 0<br>r = rclean robstacle r = rattack robstacle<br>r < 0: 97.3% 100 r < 0: 89.3%<br>100<br>50<br>50<br>0 0<br>10 0 10 0<br>r = rclean robstacle r = rattack robstacle<br>r < 0: 97.3%<br>100 r < 0: 96.9%<br>75<br>50<br>50<br>25<br>0 0<br>10 5 0 10 5 0<br>r = rclean robstacle r = rattack robstacle<br>r < 0: 93.8%<br>100 r < 0: 97.8%<br>75<br>50<br>50<br>25<br>0 0<br>20 10 0 10 0<br>r = rclean robstacle r = rattack robstacle<br>Sample Count Sample Count<br>Sample Count Sample Count<br>Sample Count Sample Count<br>Sample Count Sample Count<br>**----- End of picture text -----**<br>


Fig. 10: Diversity difference distributions (∆ _r_ ) for four representative intermediate layer outputs. Left: ∆Benign = _r_ empty _− r_ occupy; Right: ∆Attack = _r_ attack _− r_ occupy. Top to bottom: the norm1 output of ViT-0 block 0 (selected, strong regularity); the cross_att output of ViT-0 block 0 (rejected, pre-normalization energy-dominated); the norm1 output of ViT-1 block 2 (selected, highest Empty-condition rate in ViT-1); the norm2 output of ViT-2 block 7 (rejected, near-random distribution at early pipeline stage). The vertical line at ∆ _r_ = 0 separates diversity increase (positive) from collapse (negative). 

## V. REAL-WORLD VALIDATION 

We conducted real-world experiments to evaluate the effectiveness of the proposed adversarial taillights. All experiments were performed in an open parking lot under comprehensive safety protocols. 

## _A. Implementation_ 

_1) Vehicle with Adversarial Taillights:_ The adversarial vehicle used in our experiments was an LS-series model from IM Motors, equipped with programmable taillights—a widely sold vehicle. For safety reasons, illegal alterations 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

12 

**==> picture [82 x 47] intentionally omitted <==**

**==> picture [81 x 47] intentionally omitted <==**

**==> picture [81 x 47] intentionally omitted <==**

**==> picture [82 x 47] intentionally omitted <==**

**==> picture [81 x 47] intentionally omitted <==**

**==> picture [81 x 47] intentionally omitted <==**

Fig. 11: The visualization of the victim model’s attention on adversarial taillights using Grad-CAM. 

**==> picture [82 x 47] intentionally omitted <==**

**==> picture [81 x 47] intentionally omitted <==**

**==> picture [81 x 47] intentionally omitted <==**

**==> picture [246 x 88] intentionally omitted <==**

**----- Start of picture text -----**<br>
(a) Numt :5597 (b) Numt :11195 (c) Numt :16793<br>ASR:0.75% ASR:53% ASR:64%<br>(d) Numt :22391 (e) Numt :27989 (f) Numt :39185<br>ASR:78% ASR:93% ASR:95%<br>**----- End of picture text -----**<br>


Fig. 12: The generated adversarial taillight images with different numbers of training images( _Numt_ ) and their ASRs. 

to vehicle systems could introduce driving uncertainties, potentially compromising user safety. Upon examining the vehicle architecture, we determined that the programmable taillights are implemented using LED panels. Therefore, rather than directly modifying the vehicle systems, we adhered flexible, programmable LED panels over the existing taillight regions. These added panels resemble the original taillights and are used to display the adversarial taillight images. 

_2) Victim Vehicle Configuration:_ Due to safety considerations and practical constraints, we deployed an occupancy network-based autonomous driving system on our research vehicle rather than acquiring commercial Tesla vehicles equipped with Full Self-Driving (FSD) capabilities. The research vehicle was built on a standard chassis with Ackermann steering, retrofitted with perception sensors and onboard computing hardware. The sensor suite included a real-time kinematic (RTK) navigation system for centimeter-level localization via GNSS and IMU fusion, along with a front-facing HDR camera featuring a 6 mm focal length lens mounted at 1.5 m height with minimal pitch angle. The camera utilized a 1/3-inch sensor format to capture 1920 × 1080 pixel images at 30 fps. Computational processing was handled by an industrial in-vehicle computer equipped with an NVIDIA GeForce RTX 3060 GPU (12 GB). The vehicle was also installed with the occupancy prediction model [30]. 

The occupancy prediction model serves as the perception module in autonomous driving systems, acting as the central brain that collaborates with other modules to enable seamless autonomous driving. Hence, we integrated the occupancy 

**==> picture [119 x 89] intentionally omitted <==**

**==> picture [119 x 89] intentionally omitted <==**

Fig. 13: The real-world attack experiements. 

prediction model [30] into an open source autonomous driving system [41] as the percetion module. The occupancy prediction model outputs the prediction results of grids where each grid is set as a cube with high-width-length is 0.5 m. Each grid has multi-class probability scores and the grids were identified as vehicle if the _vehicle_ class is the maximum value. We converted the contiguous regions of grids which are classified as _vehicle_ into 3D bounding boxes using connected component analysis [33]. The confidence score was calculated by averaging the _vehicle_ class probability scores across all grids within each contiguous region. The lower detection threshold of confidence score indicates the target object is more likely be detected as the vehicle. As our attack aims to cause the target object not be classified as vehicle (disappearing), the model with lower detection threshold setting is more difficult to attack. Hence, we set the threshold of confidence score is set as a lower value: 0.3, to demonstrate the effectiveness of our attack method. We forward these values to the tracking module APIs of the open source autonomous driving system [41] to achieve autonomous driving. 

For safety, the research vehicle incorporated sensitive bumper strips, an emergency braking system, and immediate intervention capabilities by on-site personnel. This vehicle served as the victim vehicle in our experiments. 

## _B. Experimental Results_ 

The evaluation comprised two stages of experiments. The first stage validated the victim vehicle’s braking capability without adversarial attacks, while the second assessed braking failure under the adversarial attacks. 

In the first stage, the adversarial vehicle was positioned ahead of the victim vehicle, with its taillights displaying a plain pattern devoid of adversarial images. Upon initiating the victim vehicle, it successfully braked in all three trials, avoiding collision. These results confirmed the occupancy prediction model’s ability to detect obstacles and initiate braking. 

In the second stage, the adversarial vehicle was similarly positioned but equipped with adversarial taillights displaying programmed adversarial images. A 20 cm thick honeycomb cardboard buffer was affixed to the adversarial vehicle’s rear bumper for safety. Upon starting the victim vehicle toward the adversarial vehicle, the perception system failed to detect the adversarial vehicle in all three trials, as shown in Fig. 13. This resulted in low-speed contact, which triggered the emergency braking system via the bumper strips. The collisions demonstrate that the adversarial taillights effectively misled the autonomous driving system. 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

13 

These findings underscore the urgent need for robust perception hardening strategies in safety-critical autonomous systems, particularly those reliant on camera-only configurations vulnerable to optical perturbations. 

## VI. RELATED WORK 

**Adversarial Attacks against Autonomous Driving.** Zhou et al. [25] proposed stealthy projector-based attacks that manipulate traffic sign appearances, demonstrating the feasibility of physical-world exploits in 2D visual systems for autonomous driving. However, these attacks are susceptible to geometric consistency checks in 3D object detection. In contrast, our approach employs programmable taillights as the attack vector, which are integral to the vehicle, thereby bypassing such consistency checks. 

Chahe et al. [42] introduced dynamic patch-based perturbations displayed on moving screens to mislead traffic sign recognition and influence vehicle decision-making. They optimized the patches to deceive the object detection models into misclassifying targeted objects. However, this method is limited to attacking traffic sign recognition. 

Wang et al. [20] developed physically realizable universal patches for camera-based 3D detection, enabling both instance-level evasion and scene-level fabrication while addressing viewpoint and distance invariance. They use a sparse object sampling strategy to ensure that the rendered patches follow the perspective criterion and avoid being occluded during training. However, they require adding static patches that are easily exposed and remain immutable once deployed. Our adversarial taillights can be dynamically activated and modified, thereby achieving superior stealthiness in physical environments. 

Chen et al. [43] presented efficient point cloud manipulation techniques for LiDAR-specific attacks, allowing adversarial object insertion and deletion. Cheng et al. [21] proposed explainability-guided perturbations that degrade tracking performance in LiDAR-based 3D object detection. Xiong et al. [44] introduced multi-sensor attacks that jointly exploit visual and LiDAR signals to compromise perception systems. Chen and Huang [45] developed adversarial textures applied to 3D objects, influencing both camera and LiDAR modalities in multi-sensor setups. However, these approaches rely on manipulating sparse geometric point clouds or LiDAR data, rendering them inapplicable to occupancy networks. 

Wang et al. [46] proposed 2D adversarial posters that fabricate non-existent objects in Bird’s-Eye-View paradigms, targeting unified spatial representations in modern vision-based 3D detection. However, their approach depends on large, static planar artifacts deployed on road surfaces, introducing severe viewpoint sensitivity and geometric instability in BEV representations. Our method utilizes the dynamic visual configurations of programmable taillights, providing intrinsic stability and superior physical stealth. 

Li et al. [47] introduced 3D adversarial meshes, such as the Meeseeks Mesh, which maintain geometric and photometric consistency across multi-camera views to exploit centralized fusion mechanisms in BEV-based perception. However, their 

perpetual visibility severely limits stealthiness, contrasting with our approach, where adversarial taillights can be dynamically activated and modified, thereby ensuring stealthiness. 

**Defenses Against Adversarial Attacks.** Liang et al. [39] proposed texture-based detection and local denoising methods to improve robustness against patch attacks. They formulate the adversarial loss to adversely affect the decision-making behavior of 3D object detection. Shibly et al. [40] utilized an autoencoder and a compressive memory module in autonomous driving perception models to store normal image features and prevent unexpected generalization on adversarial inputs against hijacking, vanishing, fabrication, and mislabeling attacks using FGSM and AdvGAN. Azim et al. [38] showed that combining Total Variance Minimization and Spatial Smoothing enhances model resilience against attacks including FGSM, BIM, and PGD. These defenses seek to eliminate high-frequency or locally irregular patterns, thereby removing adversarial perturbations. However, our adversarial taillights feature a single-color, flat, and highly smoothed design that emulates the optical properties of real vehicle taillights, leaving no high-frequency residues to remove. Consequently, these denoising defenses are ineffective against our attack. 

Sattout et al. [48] introduced a two-level detection framework using segmentation masks from U-Net and dynamic k-means for detecting and confirming perturbations against FGSM and BIM attacks. Sattout and Chehab [49] presented a method combining One-Class Support Vector Machine for anomaly detection with Siamese validation using cosine similarity, achieving high detection robustness. These strategies aim to detect adversarial perturbations by identifying regions that deviate from the surrounding context. However, our adversarial taillights closely mimic original taillights in color and geometry—monochromatic, minimalist, flat, and repetitive—rendering them hard to identify as anomalous by segmentation-based or semantic-deviation detectors. 

Zhang et al. [50] introduced Module-wise Adaptive Adversarial Training for end-to-end autonomous driving, which stabilizes multi-stage training via module-wise noise injection and dynamic weight accumulation against white-box and black-box attacks. Liu et al. [51] applied robust training methodologies to Unmanned Surface Vehicles (USVs) using adversarial reinforcement learning to train policies resilient to adversarial obstacles. These adversarial training approaches depend on samples from pixel-level, gradient-based perturbations that directly alter input images. Conversely, our GAN-based generator, constrained by color, creates natural, globally coherent adversarial taillights that significantly differ from perturbation-based artifacts. Since such samples are missing from the training distribution, the models show poor generalization to our attacks. 

Xie et al. [52] conducted a comprehensive analysis of the adversarial robustness of camera-based 3D object detection under diverse perturbations, determining that BEV-based representations and methods without depth estimation generally demonstrate greater resilience. Carlini et al. [53] introduced AutoAdvExBench, a meta-benchmark for assessing the exploitability of adversarial defenses by autonomous 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

14 

agents, highlighting a significant disparity between synthetic benchmarks and real-world robustness. Existing system-level robustness evaluations and benchmarks mainly focus on synthetic or gradient-derived perturbations to uncover model vulnerabilities. Nevertheless, they overlook adversarial taillights that emulate the visual design of commercial full-width taillights, enabling our adversarial taillights to maintain high visual coherence in driving scenarios and thus evade detection. 

## VII. CONCLUSION 

We uncover a novel collision vulnerability in the occupancy network, widely deployed in autonomous driving systems such as Tesla’s, through physical-world attacks employing programmable vehicle taillights. We model this as an optimization problem to generate realistic adversarial taillights that evade detection and induce crashes. By fine-tuning a pre-trained GAN to align its latent space with adversarial samples via gradient guidance—while incorporating color constraints, geometric transformations, and photometric augmentations—we ensure natural appearance and robustness. Extensive evaluations in simulated and real-world settings demonstrate an average attack success rate of 93%, reaching 100% under optimal conditions, while preserving stealth and durability; real-vehicle deployments confirm induced collisions. These findings underscore the urgent need for enhanced defenses against such threats, with future work exploring multi-sensor integrations and countermeasure designs. 

## REFERENCES 

- [1] T. in Asia. (2025) Xiaomi recalls nearly 117k su7 evs in china after fatal crash. Tech in Asia. [Online]. Available: https://www.techinasia.com/news/xiaomi-recalls-117k-s u7-evs-china-fatal-crash 

- [2] Tesla, “Tesla ai day 2022,” 2022. [Online]. Available: https://www.youtube.com/watch?v=ODSJsviD_SU 

- [3] National Highway Traffic Safety Administration. (2026, 3) Engineering analysis EA26002: Tesla Full Self-Driving (FSD) Degradation Detection System. NHTSA. [Online]. Available: https://static.nhtsa.gov/odi/inv/2026/INOA-E A26002-10023.pdf 

- [4] D. Ingram. (2025, 9) Tesla’s ’self-driving’ software fails at train crossings, some car owners warn. NBC News. [Online]. Available: https://www.nbcnews.com/tech/elo n-musk/tesla-full-self-driving-fails-train-crossings-drive rs-warn-railroad-rcna225558 

- [5] Aionlinecourse. (2023) Perception for self-driving cars | self driving cars. [Online]. Available: https: //www.aionlinecourse.com/tutorial/self-driving-cars/perc eption-for-self-driving-cars 

- [6] H. Xu, J. Chen, S. Meng, Y. Wang, and L.-P. Chau, “A survey on occupancy perception for autonomous driving: The information fusion perspective,” _Information Fusion_ , vol. 114, 2025. 

- [7] W. Tong, C. Sima, T. Wang, L. Chen, S. Wu, H. Deng, Y. Gu, L. Lu, P. Luo, D. Lin, and H. Li, “Scene as occupancy,” in _IEEE/CVF International Conference on Computer Vision (ICCV)_ . IEEE, 2023. 

- [8] Y. Ma, T. Wang, X. Bai, H. Yang, Y. Hou, Y. Wang, Y. Qiao, R. Yang, and X. Zhu, “Vision-Centric BEV Perception: A Survey,” _IEEE Transactions on Pattern Analysis and Machine Intelligence_ , vol. 46, 2024. 

- [9] P. Kuchár, R. Pirník, A. Janota, B. Malobický, J. Kubík, and D. Šišmišová, “Passenger occupancy estimation in vehicles: A review of current methods and research challenges,” _Sustainability_ , vol. 15, 2023. 

- [10] Tesla, “Tesla ai day 2021,” 2021. [Online]. Available: https://www.youtube.com/watch?v=j0z4FweCy4M 

- [11] H. Wei, H. Tang, X. Jia, Z. Wang, H. Yu, Z. Li, S. Satoh, L. Van Gool, and Z. Wang, “Physical adversarial attack meets computer vision: A decade survey,” _IEEE Transactions on Pattern Analysis and Machine Intelligence_ , vol. 46, 2024. 

- [12] X. Han, G. Xu, Y. Zhou, X. Yang, J. Li, and T. Zhang, “Physical backdoor attacks to lane detection systems in autonomous driving,” in _Proceedings of the 30th ACM International Conference on Multimedia_ , 2022. 

- [13] T. Sato, J. Shen, N. Wang, Y. Jia, X. Lin, and Q. A. Chen, “Dirty road can attack: Security of deep learning based automated lane centering under _{_ Physical-World _}_ attack,” in _30th USENIX security symposium (USENIX Security 21)_ , 2021. 

- [14] P. Jing, Q. Tang, Y. Du, L. Xue, X. Luo, T. Wang, S. Nie, and S. Wu, “Too good to be safe: Tricking lane detection in autonomous driving with crafted perturbations,” in _30th USENIX Security Symposium (USENIX Security 21)_ , 2021. 

- [15] X. Zhang, A. Liu, T. Zhang, S. Liang, and X. Liu, “Towards robust physical-world backdoor attacks on lane detection,” in _Proceedings of the 32nd ACM International Conference on Multimedia_ , 2024. 

- [16] P. MohajerAnsari, A. Salarpour, J. d. Voor, A. Domeke, A. Mitra, G. Johnson, H. Olufowobi, M. Hamad, and M. D. Pese, “Discovering new shadow patterns for black-box attacks on lane detection of autonomous vehicles,” 2025. [Online]. Available: http://arxiv.org/abs/2409.18248 

- [17] H. Xu, A. Ju, and D. Wagner, “Model-agnostic defense for lane detection against adversarial attack,” 2021. [Online]. Available: http://arxiv.org/abs/2103.00663 

- [18] Y. Zhao, H. Zhu, R. Liang, Q. Shen, S. Zhang, and K. Chen, “Seeing isn’t believing: Towards more robust adversarial attack against real world object detectors,” in _Proceedings of the 2019 ACM SIGSAC conference on computer and communications security_ , 2019. 

- [19] Y. Huang, Y. Dong, S. Ruan, X. Yang, H. Su, and X. Wei, “Towards transferable targeted 3d adversarial attack in the physical world,” in _Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition_ , 2024. 

- [20] J. Wang, F. Li, and L. He, “A unified framework for adversarial patch attacks against visual 3D object detection in autonomous driving,” _IEEE Transactions on Circuits and Systems for Video Technology_ , vol. 35, 2025. 

- [21] R. Cheng, X. Wang, F. Sohel, and H. Lei, “Black-box explainability-guided adversarial attack for 3D object tracking,” _IEEE Transactions on Circuits and Systems for_ 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

15 

_Video Technology_ , vol. 35, 2025. 

- [22] X. Wei, Y. Guo, and J. Yu, “Adversarial sticker: A stealthy attack method in the physical world,” _IEEE Transactions on Pattern Analysis and Machine Intelligence_ , 2022. 

- [23] S. Yang, Z. Wang, D. Ortiz, L. Burbano, M. Kantarcioglu, A. Cardenas, and C. Xie, “Probing vulnerabilities of vision-lidar based autonomous driving systems,” in _Proceedings of the Computer Vision and Pattern Recognition Conference_ , 2025. 

- [24] Y. Cao, N. Wang, C. Xiao, D. Yang, J. Fang, R. Yang, Q. A. Chen, M. Liu, and B. Li, “Invisible for both camera and lidar: Security of multi-sensor fusion based perception in autonomous driving under physical-world attacks,” in _2021 IEEE symposium on security and privacy (SP)_ , 2021. 

- [25] M. Zhou, W. Zhou, J. Huang, J. Yang, M. Du, and Q. Li, “Stealthy and effective physical adversarial attacks in autonomous driving,” _IEEE Transactions on Information Forensics and Security_ , vol. 19, 2024. 

- [26] carnewschina, “The sales of vehicles.” 2025. [Online]. Available: https://carnewschina.com/ 

- [27] D. Y. Chen, “Saic’s new im ls6,” Online, 2025, accessed: 2025-10-16. [Online]. Available: https://carnewschina.c om/2025/08/15/saics-new-im-ls6-with-catls-freevoy-sup er-max-battery-starts-pre-sale/ 

- [28] The Autopian, “The boldest taillight innovations are happening in china right now,” Online, 2024, accessed: 2025-10-16. [Online]. Available: https://www.theautopia n.com/the-boldest-taillight-innovations-are-happening-i n-china-right-now/ 

- [29] A. Sauer, K. Schwarz, and A. Geiger, “StyleGAN-XL: Scaling StyleGAN to large diverse datasets,” in _Special Interest Group on Computer Graphics and Interactive Techniques Conference Proceedings_ . ACM, 2022. 

- [30] Y. Wei, L. Zhao, W. Zheng, Z. Zhu, J. Zhou, and J. Lu, “SurroundOcc: Multi-Camera 3D Occupancy Prediction for Autonomous Driving,” 2023. [Online]. Available: http://arxiv.org/abs/2303.09551 

- [31] Y. Tang, K. Han, C. Xu, A. Xiao, Y. Deng, C. Xu, and Y. Wang, “Augmented shortcuts for vision transformers,” _Advances in Neural Information Processing Systems_ , vol. 34, pp. 15 316–15 327, 2021. 

- [32] H. Caesar, V. Bankiti, A. H. Lang, S. Vora, V. L. Liong, Q. Xu, A. Krishnan, Y. Pan, G. Baldan, and O. Beijbom, “nuscenes: A multimodal dataset for autonomous driving,” in _Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)_ , 2020. 

- [33] W. Silversmith, “connected-components-3d: Connected components on 2d and 3d images,” https://pypi.org/pro ject/connected-components-3d/1.12.0/, 2021, version 1.12.0. 

- [34] W. Xu, D. Evans, and Y. Qi, “Feature squeezing: Detecting adversarial examples in deep neural networks,” in _Proceedings 2018 Network and Distributed System Security Symposium_ , 2018. 

- [35] V. Ziyadinov and M. Tereshonok, “Low-pass image filtering to achieve adversarial robustness,” _Sensors_ , vol. 23, no. 22, p. 9032, 2023. 

- [36] C. Guo, M. Rana, M. Cisse, and L. van der 

Maaten, “Countering adversarial images using input transformations,” 2018. [Online]. Available: https: //arxiv.org/abs/1711.00117 

- [37] P. Tian, S. Poreddy, C. Danda, C. Gowrineni, Y. Wu, and W. Liao, “Evaluating impact of image transformations on adversarial examples,” _IEEE Access_ , vol. 12, 2024. 

- [38] O. A. Azim, L. Baker, R. Majumder, A. Enan, S. M. Khan, and M. A. Chowdhury, “Data-driven defenses against adversarial attacks for autonomous vehicles,” in _2023 IEEE International Automated Vehicle Validation Conference (IAVVC)_ . IEEE, 2023. 

- [39] J. Liang, R. Yi, J. Chen, Y. Nie, and H. Zhang, “Securing autonomous vehicles visual perception: Adversarial patch attack and defense schemes with experimental validations,” _IEEE Transactions on Intelligent Vehicles_ , vol. 9, 2024. 

- [40] K. H. Shibly, M. D. Hossain, H. Inoue, Y. Taenaka, and Y. Kadobayashi, “Towards autonomous driving model resistant to adversarial attack,” _Applied Artificial Intelligence_ , vol. 37, 2023. 

- [41] Baidu, “Apollo-self-driving,” Online, 2025. [Online]. Available: https://www.apollo.auto/apollo-self-driving 

- [42] A. Chahe, C. Wang, A. Jeyapratap, K. Xu, and L. Zhou, “Dynamic adversarial attacks on autonomous driving systems,” 2024. [Online]. Available: http: //arxiv.org/abs/2312.06701 

- [43] H. Chen, H. Yan, X. Yang, H. Su, S. Zhao, and F. Qian, “Efficient adversarial attack strategy against 3D object detection in autonomous driving systems,” _IEEE Transactions on Intelligent Transportation Systems_ , vol. 25, 2024. 

- [44] Z. Xiong, H. Xu, W. Li, and Z. Cai, “Multi-source adversarial sample attack on autonomous vehicles,” _IEEE Transactions on Vehicular Technology_ , vol. 70, 2021. 

- [45] C. Chen and T. Huang, “Camdar-adv: Generating adversarial patches on 3D object,” _International Journal of Intelligent Systems_ , vol. 36, 2021. 

- [46] J. Wang, F. Li, S. Lv, L. He, and C. Shen, “Physically Realizable Adversarial Creating Attack Against Vision-Based BEV Space 3D Object Detection,” _IEEE Transactions on Image Processing_ , vol. 34, 2025. 

- [47] A. Li, M. Xiang, J. Zhang, and Y. Dai, “The meeseeks mesh: Spatially consistent 3D adversarial objects for BEV detector,” 2025. [Online]. Available: http://arxiv.org/abs/2505.22499 

- [48] A. F. A. Sattout, A. Chehab, A. Mohanna, and R. Tajeddine, “Image segmentation framework for detecting adversarial attacks for autonomous driving cars,” _Applied Sciences_ , vol. 15, 2025. 

- [49] A. F. A. Sattout and A. Chehab, “OCSVM-siamese framework for detecting adversarial attacks for autonomous driving cars,” in _2025 International Wireless Communications and Mobile Computing (IWCMC)_ . IEEE, 2025. 

- [50] T. Zhang, L. Wang, J. Kang, X. Zhang, S. Liang, Y. Chen, A. Liu, and X. Liu, “Module-wise adaptive adversarial training for end-to-end autonomous driving,” 2024. [Online]. Available: http://arxiv.org/abs/2409.07321 

- [51] J. Liu, Y. Wang, and C. Sun, “Unmanned surface vehicle 

IEEE TRANSACTIONS ON _XXXX_ , VOL. XX, NO. XX, MONTH 2025 

16 

   - autonomous racing and obstacle avoidance with robust adversarial deep reinforcement learning,” _Engineering Applications of Artificial Intelligence_ , vol. 161, 2025. 

- [52] S. Xie, Z. Li, Z. Wang, and C. Xie, “On the adversarial robustness of camera-based 3D object detection,” 2024. [Online]. Available: http://arxiv.org/abs/2301.10766 

- [53] N. Carlini, J. Rando, E. Debenedetti, M. Nasr, and F. Tramèr, “AutoAdvExBench: Benchmarking autonomous exploitation of adversarial example defenses,” 2025. [Online]. Available: http://arxiv.org/abs/2503.01811 

